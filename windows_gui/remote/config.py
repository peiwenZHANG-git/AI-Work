"""Explicit, fail-closed LAN configuration for the Remote transport."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_LAN_PORT = 8933
_FORBIDDEN_PORTS = {8931, 8932}
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
))


@dataclass(frozen=True)
class LanConfig:
    """An explicit opt-in LAN binding; the default is disabled."""

    enabled: bool = False
    interface_id: str = ''
    bind_ip: str = ''
    port: int = DEFAULT_LAN_PORT
    allowed_remote_subnet: str = ''

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> 'LanConfig':
        values = values or {}
        unknown = set(values) - {
            'enabled', 'interface_id', 'bind_ip', 'port',
            'allowed_remote_subnet',
        }
        if unknown:
            raise ValueError(f'unsupported LAN settings: {", ".join(sorted(unknown))}')
        enabled = values.get('enabled', False)
        if not isinstance(enabled, bool):
            raise ValueError('LAN enabled must be a boolean')
        return cls(
            enabled=enabled,
            interface_id=str(values.get('interface_id', '')),
            bind_ip=str(values.get('bind_ip', '')),
            port=int(values.get('port', DEFAULT_LAN_PORT)),
            allowed_remote_subnet=str(values.get('allowed_remote_subnet', '')),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.interface_id or len(self.interface_id) > 128:
            raise ValueError('LAN interface_id is required')
        if self.port != DEFAULT_LAN_PORT:
            raise ValueError(f'LAN port must be {DEFAULT_LAN_PORT}')
        address = ipaddress.ip_address(self.bind_ip)
        if address.version != 4 or not any(
            address in network for network in _PRIVATE_NETWORKS
        ):
            raise ValueError('LAN bind_ip must be an explicit private IPv4 address')
        if (
            address.is_link_local or address.is_multicast
            or address.is_reserved or address.is_unspecified
        ):
            raise ValueError('LAN bind_ip is not a permitted unicast address')
        subnet = ipaddress.ip_network(self.allowed_remote_subnet, strict=False)
        if subnet.version != 4 or not any(
            subnet.subnet_of(network) for network in _PRIVATE_NETWORKS
        ) or subnet.num_addresses < 2:
            raise ValueError('allowed_remote_subnet must be a private IPv4 subnet')
        if subnet.is_loopback or subnet.is_link_local or subnet.is_multicast:
            raise ValueError('allowed_remote_subnet is not permitted')
        if address not in subnet:
            raise ValueError('bind_ip is outside allowed_remote_subnet')


def firewall_commands(config: LanConfig, python_executable: str) -> list[str]:
    """Return commands for a human administrator; never execute them."""

    config.validate()
    if not config.enabled:
        return []
    if not python_executable or any(
        character in python_executable for character in ('"', '\r', '\n')
    ):
        raise ValueError('python executable path cannot be represented safely')
    common = (
        f'name="AI-Work Remote LAN {config.port}" '
        f'dir=in action=allow program="{python_executable}" '
        f'protocol=TCP localport={config.port} '
        f'remoteip={config.allowed_remote_subnet} profile=private enable=yes'
    )
    return [
        f'netsh advfirewall firewall add rule {common}',
        f'netsh advfirewall firewall delete rule name="AI-Work Remote LAN {config.port}"',
    ]
