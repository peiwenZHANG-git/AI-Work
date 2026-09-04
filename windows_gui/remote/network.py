"""Bounded network-state validation for the opt-in LAN listener."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

from .config import LanConfig


@dataclass(frozen=True)
class InterfaceSnapshot:
    interface_id: str
    ip_address: str
    status: str
    profile: str
    hardware_interface: bool
    interface_description: str


def _default_snapshot(config: LanConfig) -> InterfaceSnapshot | None:
    """Read Windows state; any incomplete result is treated as unusable."""

    try:
        address = subprocess.run(
            [
                'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                'Get-NetIPAddress -IPAddress $env:AI_WORK_BIND_IP '
                '| Select-Object InterfaceIndex,InterfaceAlias,IPAddress,AddressState '
                '| ConvertTo-Json -Compress',
            ],
            capture_output=True, text=True, timeout=3, check=False,
            env={**os.environ, 'AI_WORK_BIND_IP': config.bind_ip},
        )
        if address.returncode != 0:
            return None
        address_data = json.loads(address.stdout)
        index = int(address_data['InterfaceIndex'])
        profile = subprocess.run(
            [
                'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                f'Get-NetConnectionProfile -InterfaceIndex {index} '
                '| Select-Object Name,NetworkCategory,IPv4Connectivity '
                '| ConvertTo-Json -Compress',
            ],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if profile.returncode != 0:
            return None
        profile_data = json.loads(profile.stdout)
        adapter = subprocess.run(
            [
                'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                f'Get-NetAdapter -InterfaceIndex {index} '
                '| Select-Object InterfaceDescription,HardwareInterface '
                '| ConvertTo-Json -Compress',
            ],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if adapter.returncode != 0:
            return None
        adapter_data = json.loads(adapter.stdout)
        categories = {0: 'Public', 1: 'Private', 2: 'Domain'}
        return InterfaceSnapshot(
            interface_id=str(address_data['InterfaceAlias']),
            ip_address=str(address_data['IPAddress']),
            status=str(address_data['AddressState']),
            profile=categories.get(int(profile_data['NetworkCategory']), 'Unknown'),
            hardware_interface=adapter_data.get('HardwareInterface') is True,
            interface_description=str(adapter_data.get('InterfaceDescription') or ''),
        )
    except Exception:
        return None


def validate_snapshot(config: LanConfig, snapshot: InterfaceSnapshot | None) -> str:
    """Return empty when valid, otherwise a stable reason without host details."""

    if snapshot is None:
        return 'network_unavailable'
    if snapshot.interface_id != config.interface_id:
        return 'interface_changed'
    if snapshot.ip_address != config.bind_ip:
        return 'address_changed'
    if snapshot.status.casefold() != 'preferred':
        return 'address_not_ready'
    if snapshot.profile.casefold() != 'private':
        return 'network_not_private'
    if not snapshot.hardware_interface:
        return 'virtual_interface_denied'
    description = snapshot.interface_description.casefold()
    if not description:
        return 'interface_type_unknown'
    if any(marker in description for marker in (
        'vpn', 'tunnel', 'tap-', 'tap ', 'wireguard', 'openvpn', 'virtual',
        'wi-fi direct', 'wifi direct', 'mobile hotspot', 'hosted network',
    )):
        return 'unsafe_interface_type'
    return ''


class NetworkMonitor:
    """Stop the LAN listener when the approved network binding changes."""

    def __init__(
        self,
        config: LanConfig,
        on_invalid: Callable[[str], None],
        *,
        collector: Callable[[LanConfig], InterfaceSnapshot | None] = _default_snapshot,
        interval_seconds: float = 5.0,
    ) -> None:
        self._config = config
        self._on_invalid = on_invalid
        self._collector = collector
        self._interval = float(interval_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        reason = validate_snapshot(self._config, self._collector(self._config))
        if reason:
            raise ValueError(reason)
        self._thread = threading.Thread(
            target=self._run, name='remote-lan-network-monitor', daemon=True,
        )
        self._thread.start()
        return ''

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            reason = validate_snapshot(self._config, self._collector(self._config))
            if reason:
                self._on_invalid(reason)
                self._stop_event.set()
                return

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=2.0)
            self._thread = None
