"""CLI entry for the Remote transport; LAN is an explicit opt-in."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from windows_gui.remote.server import DEFAULT_PORT, RemoteServer
from windows_gui.remote.config import LanConfig, firewall_commands
from windows_gui.remote.pairing import PendingPairingManager
from windows_gui.remote.local_plane import DEFAULT_LOCAL_PLANE_PORT, LocalPlaneServer
from windows_gui.remote.lan_server import LanServer
from windows_gui.remote.tls import TlsManager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Run the AI-Work Remote API server (LAN is opt-in)',
    )
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--lan-config', type=Path)
    parser.add_argument('--local-plane-port', type=int, default=DEFAULT_LOCAL_PLANE_PORT)
    parser.add_argument('--print-firewall-commands', action='store_true')
    arguments = parser.parse_args(argv)
    lan_values = None
    if arguments.lan_config:
        try:
            lan_values = json.loads(arguments.lan_config.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            print('invalid_lan_config', flush=True)
            return 2
    config = LanConfig.from_mapping(lan_values)
    try:
        config.validate()
    except ValueError as error:
        print('invalid_lan_config', flush=True)
        return 2
    if arguments.print_firewall_commands:
        for command in firewall_commands(config, sys.executable):
            print(command, flush=True)
        return 0
    server = RemoteServer(port=arguments.port)
    server.start()
    print(f'AI-Work Remote API listening on 127.0.0.1:{server.port}', flush=True)
    local_plane = None
    lan_server = None
    pairing = None
    try:
        if config.enabled:
            pairing = PendingPairingManager(server.authenticator)
            lan_server = LanServer(
                config=config,
                remote=server,
                pairing=pairing,
                tls_manager=TlsManager(),
            )
            lan_server.start()
            local_plane = LocalPlaneServer(
                remote=server,
                pairing=pairing,
                port=arguments.local_plane_port,
                lan_bootstrap=lambda: {
                    'endpoint': f'{config.bind_ip}:{lan_server.port}',
                    'spki_sha256': lan_server.tls_material.spki_sha256,
                },
            )
            local_plane.start()
            print(
                f'AI-Work Remote LAN TLS listening on '
                f'{config.bind_ip}:{lan_server.port}; SPKI SHA-256='
                f'{lan_server.tls_material.spki_sha256}',
                flush=True,
            )
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        if lan_server is not None:
            lan_server.stop()
        if local_plane is not None:
            local_plane.stop()
        server.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
