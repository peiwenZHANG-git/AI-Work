import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.remote_server import main as remote_server_main
from windows_gui.remote.config import LanConfig, firewall_commands


def valid_values(**overrides):
    values = {
        'enabled': True,
        'interface_id': 'Ethernet',
        'bind_ip': '192.168.10.20',
        'port': 8933,
        'allowed_remote_subnet': '192.168.10.0/24',
    }
    values.update(overrides)
    return values


class LanConfigTests(unittest.TestCase):
    def test_default_is_disabled_and_valid_without_network_values(self):
        config = LanConfig.from_mapping(None)
        self.assertFalse(config.enabled)
        config.validate()

    def test_valid_private_explicit_binding_is_accepted(self):
        LanConfig.from_mapping(valid_values()).validate()

    def test_unknown_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            LanConfig.from_mapping({'auto_select': True})

    def test_enabled_flag_must_be_a_json_boolean(self):
        with self.assertRaises(ValueError):
            LanConfig.from_mapping({'enabled': 'false'})

    def test_enabled_config_requires_complete_binding(self):
        with self.assertRaises(ValueError):
            LanConfig(enabled=True).validate()

    def test_wildcard_loopback_ipv6_public_and_reserved_are_rejected(self):
        for bind_ip in ('0.0.0.0', '127.0.0.1', '::', '203.0.113.10', '192.0.2.10'):
            with self.subTest(bind_ip=bind_ip):
                with self.assertRaises(ValueError):
                    LanConfig.from_mapping(valid_values(bind_ip=bind_ip)).validate()

    def test_port_is_fixed_and_subnet_must_contain_bind_ip(self):
        with self.assertRaises(ValueError):
            LanConfig.from_mapping(valid_values(port=8932)).validate()
        with self.assertRaises(ValueError):
            LanConfig.from_mapping(valid_values(
                allowed_remote_subnet='10.0.0.0/8',
            )).validate()

    def test_firewall_commands_are_print_only_and_scoped(self):
        config = LanConfig.from_mapping(valid_values())
        commands = firewall_commands(config, 'C:/python.exe')
        self.assertEqual(2, len(commands))
        self.assertIn('add rule', commands[0])
        self.assertIn('remoteip=192.168.10.0/24', commands[0])
        self.assertIn('profile=private', commands[0])
        self.assertIn('delete rule', commands[1])
        self.assertEqual([], firewall_commands(LanConfig(), 'python'))
        with self.assertRaises(ValueError):
            firewall_commands(config, 'C:/bad"path/python.exe')

    def test_firewall_guidance_mode_never_starts_a_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lan.json'
            path.write_text(json.dumps(valid_values()), encoding='utf-8')
            with patch('scripts.remote_server.RemoteServer') as remote_server:
                self.assertEqual(0, remote_server_main([
                    '--lan-config', str(path), '--print-firewall-commands',
                ]))
            remote_server.assert_not_called()


if __name__ == '__main__':
    unittest.main()
