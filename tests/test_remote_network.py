import threading
import unittest

from windows_gui.remote.config import LanConfig
from windows_gui.remote.network import NetworkMonitor, InterfaceSnapshot, validate_snapshot


def config():
    return LanConfig(
        enabled=True,
        interface_id='Ethernet',
        bind_ip='192.168.10.20',
        port=8933,
        allowed_remote_subnet='192.168.10.0/24',
    )


def snapshot(**overrides):
    values = {
        'interface_id': 'Ethernet',
        'ip_address': '192.168.10.20',
        'status': 'Preferred',
        'profile': 'Private',
        'hardware_interface': True,
        'interface_description': 'Intel Ethernet Adapter',
    }
    values.update(overrides)
    return InterfaceSnapshot(**values)


class NetworkValidationTests(unittest.TestCase):
    def test_explicit_private_physical_interface_is_accepted(self):
        self.assertEqual('', validate_snapshot(config(), snapshot()))

    def test_identity_address_state_and_profile_changes_fail_closed(self):
        cases = (
            ({'interface_id': 'Wi-Fi'}, 'interface_changed'),
            ({'ip_address': '192.168.10.21'}, 'address_changed'),
            ({'status': 'Tentative'}, 'address_not_ready'),
            ({'profile': 'Public'}, 'network_not_private'),
            ({'profile': 'Unknown'}, 'network_not_private'),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(reason, validate_snapshot(config(), snapshot(**overrides)))

    def test_vpn_hotspot_and_virtual_interfaces_fail_closed(self):
        cases = (
            ({'hardware_interface': False}, 'virtual_interface_denied'),
            ({'interface_description': 'WireGuard Tunnel'}, 'unsafe_interface_type'),
            ({'interface_description': 'Microsoft Wi-Fi Direct Virtual Adapter'},
             'unsafe_interface_type'),
            ({'interface_description': ''}, 'interface_type_unknown'),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(reason, validate_snapshot(config(), snapshot(**overrides)))

    def test_monitor_callback_can_stop_monitor_without_joining_itself(self):
        changed = threading.Event()
        calls = {'count': 0}
        monitor = None

        def collect(_config):
            calls['count'] += 1
            if calls['count'] == 1:
                return snapshot()
            return snapshot(profile='Public')

        def invalid(_reason):
            monitor.stop()
            changed.set()

        monitor = NetworkMonitor(
            config(), invalid, collector=collect, interval_seconds=0.01,
        )
        monitor.start()
        self.addCleanup(monitor.stop)
        self.assertTrue(changed.wait(1.0))


if __name__ == '__main__':
    unittest.main()
