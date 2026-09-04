"""Opt-in TLS listener for the LAN Remote API."""

from __future__ import annotations

import json
import ipaddress
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .config import LanConfig
from .auth import RemoteAuthError
from .network import InterfaceSnapshot, NetworkMonitor, validate_snapshot, _default_snapshot
from .pairing import PairingError, PendingPairingManager
from .policy import LIMITS
from .protocol import MAX_BODY_BYTES
from .server import RemoteApiError, RemoteServer
from .tls import TlsManager, server_ssl_context


class _LanHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class _LanRequestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, server: 'LanServer', **kwargs) -> None:
        self.lan = server
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):  # noqa: N802 - no request logging
        pass

    def _guard(self) -> bool:
        expected = f'{self.lan.config.bind_ip}:{self.lan.port}'
        if (self.headers.get('Host') or '').casefold() != expected:
            self._send_error(403, 'forbidden')
            return False
        origin = self.headers.get('Origin')
        if origin and origin != f'https://{expected}':
            self._send_error(403, 'forbidden')
            return False
        try:
            source = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            self._send_error(403, 'forbidden')
            return False
        if source.version != 4 or source not in self.lan.allowed_network:
            self._send_error(403, 'forbidden')
            return False
        return True

    def _path(self) -> str:
        value = self.path
        if '?' in value or '#' in value:
            raise RemoteApiError(400, 'invalid_request')
        return value

    def _read_json_body(self) -> tuple[bytes, dict[str, Any]]:
        content_type = (
            self.headers.get('Content-Type', '')
            .split(';', 1)[0].strip().casefold()
        )
        if content_type != 'application/json':
            raise RemoteApiError(400, 'invalid_request')
        length_header = self.headers.get('Content-Length')
        try:
            length = int(length_header or '')
        except ValueError as error:
            raise RemoteApiError(400, 'invalid_request') from error
        if length < 0 or length > MAX_BODY_BYTES:
            raise RemoteApiError(413, 'request_too_large')
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as error:
            raise RemoteApiError(400, 'invalid_request') from error
        if not isinstance(payload, dict):
            raise RemoteApiError(400, 'invalid_request')
        return body, payload

    def _read_json(self) -> dict[str, Any]:
        return self._read_json_body()[1]

    def _bearer(self) -> str:
        header = self.headers.get('Authorization') or ''
        if not header.startswith('Bearer '):
            raise RemoteApiError(401, 'auth_failed')
        return header[len('Bearer '):].strip()

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Cache-Control', 'no-store')
        self.send_header(
            'Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'",
        )
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: int, code: str) -> None:
        self.close_connection = True
        self._send_json({'error': code}, status)

    def _run(self, action: Callable[[], tuple[int, dict[str, Any]]]) -> None:
        try:
            status, payload = action()
        except RemoteApiError as error:
            self._send_error(error.status, error.code)
        except PairingError as error:
            self._send_error(403, str(error))
        except Exception:
            self._send_error(500, 'internal_error')
        else:
            self._send_json(payload, status)

    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        try:
            path = self._path()
        except RemoteApiError as error:
            self._send_error(error.status, error.code)
            return
        if path == '/health':
            def action():
                device_id = self.headers.get('X-Remote-Device') or ''
                try:
                    self.lan.remote.authenticator.authenticate(
                        device_id,
                        self.headers.get('X-Remote-Nonce') or '',
                        self.headers.get('X-Remote-Timestamp') or '',
                        self.headers.get('X-Signature') or '',
                        'GET', '/health', b'', signed_device_id=device_id,
                    )
                except RemoteAuthError as error:
                    raise RemoteApiError(401, 'auth_failed') from error
                session_device = self.lan.remote._require_session_device(
                    self._bearer(), device_id,
                )
                if session_device != device_id:
                    raise RemoteApiError(401, 'auth_failed')
                self.lan.remote._check_rate_limits(device_id, 'health_read')
                return self.lan.remote._command_health_read(device_id, '', {})
            self._run(action)
            return
        self._send_error(404, 'not_found')

    def do_POST(self):  # noqa: N802
        if not self._guard():
            return
        try:
            path = self._path()
        except RemoteApiError as error:
            self._send_error(error.status, error.code)
            return
        if path == '/session':
            def action():
                body, _ = self._read_json_body()
                return self.lan.remote.handle_session(
                    self.headers.get('X-Remote-Device') or '',
                    self.headers.get('X-Remote-Nonce') or '',
                    self.headers.get('X-Remote-Timestamp') or '',
                    self.headers.get('X-Signature') or '',
                    body,
                )
            self._run(action)
            return
        if path == '/command':
            def action():
                body, _ = self._read_json_body()
                return self.lan.remote.handle_command(
                    body,
                    device_id=self.headers.get('X-Remote-Device') or '',
                    nonce=self.headers.get('X-Remote-Nonce') or '',
                    timestamp=self.headers.get('X-Remote-Timestamp') or '',
                    signature=self.headers.get('X-Signature') or '',
                    session_token=self._bearer(),
                )
            self._run(action)
            return
        if path == '/pairing/pending':
            def action():
                payload = self._read_json()
                if set(payload) - {'code', 'device_name'}:
                    raise RemoteApiError(400, 'invalid_request')
                source = str(self.client_address[0])
                if not self.lan.remote.limiter.allow(
                    LIMITS['pairing_pending_source'], source,
                ) or not self.lan.remote.limiter.allow(
                    LIMITS['pairing_pending_global'], 'global',
                ):
                    self.lan.remote._audit('rate_limited')
                    raise RemoteApiError(429, 'rate_limited')
                result = self.lan.pairing.create_pending(
                    payload.get('code') or '', payload.get('device_name') or '',
                    source,
                )
                result['endpoint'] = (
                    f'{self.lan.config.bind_ip}:{self.lan.port}'
                )
                return 200, result
            self._run(action)
            return
        if path == '/pairing/complete':
            def action():
                payload = self._read_json()
                if set(payload) != {'request_id', 'claim_token', 'client_nonce'}:
                    raise RemoteApiError(400, 'invalid_request')
                source = str(self.client_address[0])
                if not self.lan.remote.limiter.allow(
                    LIMITS['pairing_complete_source'], source,
                ) or not self.lan.remote.limiter.allow(
                    LIMITS['pairing_complete_global'], 'global',
                ):
                    self.lan.remote._audit('rate_limited')
                    raise RemoteApiError(429, 'rate_limited')
                return 200, self.lan.pairing.complete(
                    str(payload.get('request_id') or ''),
                    str(payload.get('claim_token') or ''),
                    str(payload.get('client_nonce') or ''),
                )
            self._run(action)
            return
        self._send_error(404, 'not_found')


class LanServer:
    """Strict TLS listener; local confirmation routes are structurally absent."""

    def __init__(
        self,
        *,
        config: LanConfig,
        remote: RemoteServer,
        pairing: PendingPairingManager,
        tls_manager: TlsManager | None = None,
        collector: Callable[[LanConfig], InterfaceSnapshot | None] = _default_snapshot,
        monitor_interval: float | None = 5.0,
        validate_config: bool = True,
    ) -> None:
        self.config = config
        self.remote = remote
        self.pairing = pairing
        self.tls_manager = tls_manager or TlsManager()
        self._collector = collector
        self._monitor_interval = monitor_interval
        self._monitor: NetworkMonitor | None = None
        self._httpd: _LanHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.allowed_network = ipaddress.ip_network(
            config.allowed_remote_subnet, strict=False,
        )
        self.port = int(config.port)
        self.tls_material = None
        self._started = False
        self._validate_config = bool(validate_config)
        if self._validate_config:
            config.validate()
            if not config.enabled:
                raise ValueError('LAN is disabled')

    def start(self) -> None:
        try:
            if self._validate_config:
                self.config.validate()
                if not self.config.enabled:
                    raise ValueError('LAN is disabled')
            snapshot = self._collector(self.config)
            reason = validate_snapshot(self.config, snapshot)
            if reason:
                raise ValueError(reason)
            material = self.tls_manager.load_or_create(
                server_id='local-ai-work', bind_ip=self.config.bind_ip,
            )
            context = server_ssl_context(material)
            handler = lambda *args, **kwargs: _LanRequestHandler(
                *args, server=self, **kwargs,
            )
            self._httpd = _LanHTTPServer((self.config.bind_ip, self.config.port), handler)
            self.port = self._httpd.server_address[1]
            self.tls_material = material
            self._httpd.socket = context.wrap_socket(
                self._httpd.socket, server_side=True,
            )
            self._thread = threading.Thread(
                target=self._httpd.serve_forever, name='remote-lan-api', daemon=True,
            )
            self._thread.start()
            if self._monitor_interval is not None:
                try:
                    self._monitor = NetworkMonitor(
                        self.config,
                        self._network_invalid,
                        collector=self._collector,
                        interval_seconds=self._monitor_interval,
                    )
                    self._monitor.start()
                except Exception:
                    self._monitor = None
                    self.stop()
                    raise
            self.remote._audit('lan_started')
            self._started = True
        except Exception as error:
            self.remote._audit('lan_bind_failed')
            if self._httpd is not None:
                self._httpd.server_close()
                self._httpd = None
            raise ValueError('LAN listener failed to start') from error

    def _network_invalid(self, reason: str) -> None:
        self.remote._audit('lan_network_changed')
        self.stop()

    def stop(self) -> None:
        was_started = self._started
        self._started = False
        if self._monitor is not None:
            self._monitor.stop()
            self._monitor = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if was_started:
            self.remote._audit('lan_stopped')
