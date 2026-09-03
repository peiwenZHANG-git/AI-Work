"""Loopback-only Remote HTTP server (Phase 3B-2, no MCP surface).

Binds 127.0.0.1 only. Every endpoint requires authentication except the
pairing endpoints added in 3B-3. Fixed JSON error codes, no caller-provided
detail in any response.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ..mail_backends import WindowsCredentialManagerSecretStore
from ..system_health import collect_dashboard_health
from .audit import record_remote_event
from .auth import (
    REMOTE_CREDENTIAL_SERVICE,
    PairingCodeExpiredError,
    PairingCodeInvalidError,
    RemoteAuthError,
    RemoteAuthenticator,
    audit_device_hash,
)
from .policy import LIMITS, RateLimiter
from .protocol import ProtocolError, UnknownCommandError, parse_request_envelope
from .protocol import IdempotencyCache


DEFAULT_PORT = 8932
MAX_BODY_BYTES = 256 * 1024


def _default_devices_path() -> Path:
    local = os.environ.get('LOCALAPPDATA')
    base = Path(local) if local else Path.home() / 'AppData' / 'Local'
    return base / 'AI-Work' / 'remote' / 'devices.json'


class RemoteApiError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _default_audit_pepper() -> str:
    store = WindowsCredentialManagerSecretStore(
        REMOTE_CREDENTIAL_SERVICE, 'audit_pepper',
    )
    pepper = store.get_secret()
    if not pepper:
        pepper = secrets.token_hex(16)
        store.set_secret(pepper)
    return pepper


class RemoteServer:
    """Single-loopback Remote API server; sessions die with the process."""

    def __init__(
        self,
        *,
        host: str = '127.0.0.1',
        port: int = DEFAULT_PORT,
        authenticator: RemoteAuthenticator | None = None,
        limiter: RateLimiter | None = None,
        health_collector: Callable[[], dict[str, Any]] = collect_dashboard_health,
        task_source: Callable[[str], list[dict[str, Any]]] | None = None,
        pepper_provider: Callable[[], str] = _default_audit_pepper,
        audit_recorder: Callable[..., bool] = record_remote_event,
        devices_path: Path | None = None,
    ) -> None:
        if host not in ('127.0.0.1', 'localhost'):
            raise ValueError('Remote server binds loopback addresses only')
        self.host = '127.0.0.1'
        self.requested_port = int(port)
        self.port = int(port)
        self.authenticator = authenticator or RemoteAuthenticator()
        self.limiter = limiter or RateLimiter()
        self._collect_health = health_collector
        self._task_source = task_source or (lambda device_id: [])
        self._pepper_provider = pepper_provider
        self._audit_recorder = audit_recorder
        self.devices_path = (
            Path(devices_path) if devices_path is not None
            else _default_devices_path()
        )
        self.idempotency = IdempotencyCache()
        self._idempotency_lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self._load_registry()
        handler = partial(_RequestHandler, remote=self)
        self._httpd = ThreadingHTTPServer((self.host, self.requested_port), handler)
        self.port = self._httpd.server_address[1]
        self._httpd.remote = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name='remote-api-server',
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    # -- device registry -------------------------------------------------

    def _load_registry(self) -> None:
        try:
            raw = self.devices_path.read_text(encoding='utf-8')
            records = json.loads(raw)
        except (OSError, ValueError):
            return
        if isinstance(records, list):
            self.authenticator.restore_devices(records)

    def _save_registry(self) -> None:
        records = [
            {
                'device_id': device.device_id,
                'name': device.name,
                'status': device.status,
                'created_at': device.created_at_iso,
            }
            for device in self.authenticator.list_devices()
        ]
        self.devices_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.devices_path.with_suffix('.json.tmp')
        temporary.write_text(
            json.dumps(records, ensure_ascii=True, indent=2),
            encoding='utf-8',
        )
        temporary.replace(self.devices_path)

    # -- helpers used by the handler -------------------------------------

    def _audit(self, code: str, *, device_id: str | None = None) -> None:
        try:
            pepper = self._pepper_provider()
        except Exception:
            pepper = None
        try:
            self._audit_recorder(code, device_id=device_id, pepper=pepper)
        except Exception:
            pass

    def handle_session(
        self,
        device_id: str,
        nonce: str,
        timestamp: str,
        signature: str,
        body: bytes,
    ) -> tuple[int, dict[str, Any]]:
        try:
            self.authenticator.authenticate(
                device_id, nonce, timestamp, signature, 'POST', '/session', body,
            )
        except RemoteAuthError as error:
            self._audit('auth_failed', device_id=device_id)
            code = {
                'ReplayError': 'replay_detected',
                'TimestampError': 'timestamp_invalid',
            }.get(type(error).__name__, 'auth_failed')
            raise RemoteApiError(401, code) from error
        if not self.limiter.allow(LIMITS['session_device'], str(device_id)):
            self._audit('rate_limited', device_id=device_id)
            raise RemoteApiError(429, 'rate_limited')
        token = self.authenticator.create_session(str(device_id))
        self._audit('session_created', device_id=device_id)
        return 200, {'session': token}

    def handle_command(
        self,
        body: bytes,
        *,
        device_id: str,
        nonce: str,
        timestamp: str,
        signature: str,
        session_token: str,
    ) -> tuple[int, dict[str, Any]]:
        try:
            self.authenticator.authenticate(
                device_id,
                nonce,
                timestamp,
                signature,
                'POST',
                '/command',
                body,
                signed_device_id=device_id,
            )
        except RemoteAuthError as error:
            self._audit('auth_failed', device_id=device_id)
            code = {
                'ReplayError': 'replay_detected',
                'TimestampError': 'timestamp_invalid',
            }.get(type(error).__name__, 'auth_failed')
            raise RemoteApiError(401, code) from error
        try:
            spec, request_id, params = parse_request_envelope(body)
        except UnknownCommandError as error:
            self._audit('command_denied', device_id=device_id)
            raise RemoteApiError(400, 'unknown_command') from error
        except ProtocolError as error:
            raise RemoteApiError(400, 'invalid_request') from error
        handlers = {
            'health.read': self._command_health_read,
            'task.status': self._command_task_status,
            'session.revoke_self': self._command_revoke_self,
        }
        handler = handlers.get(spec.name)
        if handler is None:
            self._audit('command_denied', device_id=device_id)
            raise RemoteApiError(400, 'unavailable_command')

        if spec.mutating:
            with self._idempotency_lock:
                cached = self.idempotency.get(device_id, request_id)
                if cached is not None:
                    return 200, cached

                session_device = self._require_session_device(
                    session_token, device_id,
                )
                self._check_rate_limits(device_id, spec.rate_limit)
                if spec.name == 'session.revoke_self':
                    status, payload = self._command_revoke_self(
                        device_id, session_device, session_token,
                    )
                else:
                    status, payload = handler(device_id, request_id, params)
                if status == 200:
                    self.idempotency.put(device_id, request_id, payload)
                return status, payload

        self._require_session_device(session_token, device_id)
        self._check_rate_limits(device_id, spec.rate_limit)
        return handler(device_id, request_id, params)

    def _require_session_device(
        self, session_token: str, device_id: str,
    ) -> str:
        try:
            session_device = self.authenticator.validate_session(session_token)
        except RemoteAuthError as error:
            self._audit('auth_failed', device_id=device_id)
            raise RemoteApiError(401, 'auth_failed') from error
        if not secrets.compare_digest(session_device, str(device_id)):
            self._audit('auth_failed', device_id=device_id)
            raise RemoteApiError(401, 'auth_failed')
        return session_device

    def _check_rate_limits(self, device_id: str, rate_limit: str) -> None:
        device_limit = LIMITS[f'{rate_limit}_device']
        global_limit = LIMITS.get(f'{rate_limit}_global')
        if not self.limiter.allow(device_limit, device_id) or (
            global_limit is not None
            and not self.limiter.allow(global_limit, 'global')
        ):
            self._audit('rate_limited', device_id=device_id)
            raise RemoteApiError(429, 'rate_limited')

    def _command_health_read(
        self, device_id: str, request_id: str, params: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        report = self._collect_health()
        components = [
            {'component': item.get('component'), 'status': item.get('status')}
            for item in report.get('components', [])
        ]
        return 200, {
            'overall_status': report.get('overall_status'),
            'components': components,
        }

    def _command_task_status(
        self, device_id: str, request_id: str, params: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        return 200, {'tasks': self._task_source(device_id)}

    def _command_revoke_self(
        self,
        device_id: str,
        session_device: str,
        session_token: str,
    ) -> tuple[int, dict[str, Any]]:
        if not secrets.compare_digest(session_device, str(device_id)):
            raise RemoteApiError(401, 'auth_failed')
        if not self.authenticator.revoke_session(session_token):
            raise RemoteApiError(401, 'auth_failed')
        self._audit('session_revoked', device_id=device_id)
        return 200, {'status': 'SESSION_REVOKED'}

    def _revoke_device(self, device_id: str) -> bool:
        revoked = self.authenticator.revoke_device(device_id)
        if revoked:
            self.idempotency.purge_device(device_id)
            self._audit('device_revoked', device_id=device_id)
            self._save_registry()
        return revoked

    def handle_pairing_start(self) -> tuple[int, dict[str, Any]]:
        code = self.authenticator.start_pairing()
        self._audit('pairing_started')
        return 200, {'pairing_code': code}

    def handle_pairing_claim(
        self, body: bytes, source_key: str,
    ) -> tuple[int, dict[str, Any]]:
        if not self.limiter.allow(
            LIMITS['pairing_claim_source'], source_key,
        ) or not self.limiter.allow(LIMITS['pairing_claim_global'], 'global'):
            self._audit('rate_limited')
            raise RemoteApiError(429, 'rate_limited')
        try:
            envelope = json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as error:
            raise RemoteApiError(400, 'invalid_request') from error
        if not isinstance(envelope, dict) or set(envelope) - {
            'code', 'device_name',
        }:
            raise RemoteApiError(400, 'invalid_request')
        code = envelope.get('code')
        device_name = envelope.get('device_name', '')
        if not isinstance(code, str) or (
            device_name is not None and not isinstance(device_name, str)
        ):
            raise RemoteApiError(400, 'invalid_request')
        try:
            device_id, secret = self.authenticator.claim_pairing(
                code, device_name or '',
            )
        except PairingCodeExpiredError as error:
            self._audit('pairing_failed')
            raise RemoteApiError(403, 'pairing_expired') from error
        except PairingCodeInvalidError as error:
            self._audit('pairing_failed')
            raise RemoteApiError(403, 'pairing_invalid') from error
        self._audit('pairing_completed', device_id=device_id)
        self._save_registry()
        return 200, {'device_id': device_id, 'secret': secret}

    def list_local_devices(self) -> tuple[int, dict[str, Any]]:
        devices = [
            {
                'device_id': device.device_id,
                'name': device.name,
                'status': device.status,
                'created_at': device.created_at_iso,
            }
            for device in self.authenticator.list_devices()
        ]
        return 200, {'devices': devices}

    def revoke_all_devices(self) -> tuple[int, dict[str, Any]]:
        count = self.authenticator.revoke_all_devices()
        self.idempotency.purge_all()
        self._audit('all_devices_revoked')
        self._save_registry()
        return 200, {'revoked': count}

    def _device_hash(self, device_id: str) -> str:
        try:
            pepper = self._pepper_provider()
        except Exception:
            return ''
        if not pepper:
            return ''
        return audit_device_hash(device_id, pepper)


class _RequestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, remote: RemoteServer, **kwargs) -> None:
        self.remote = remote
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):  # noqa: N802 - silence stderr
        pass

    # -- helpers ---------------------------------------------------------

    def _reject_non_loopback_host(self) -> bool:
        expected = f'127.0.0.1:{self.remote.port}'
        if (self.headers.get('Host') or '').casefold() != expected:
            self._send_error(403, 'forbidden')
            return True
        if self.headers.get('Origin'):
            self._send_error(403, 'forbidden')
            return True
        return False

    def _read_body(self) -> bytes:
        length_header = self.headers.get('Content-Length')
        try:
            length = int(length_header or '')
        except ValueError as error:
            raise RemoteApiError(400, 'invalid_request') from error
        if length < 0:
            raise RemoteApiError(400, 'invalid_request')
        if length > MAX_BODY_BYTES:
            # Drain a bounded amount so well-behaved clients can still read
            # the fixed 413 response instead of a connection reset.
            remaining = min(length, 1024 * 1024)
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass
            raise RemoteApiError(413, 'request_too_large')
        return self.rfile.read(length)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=True).encode('utf-8')
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

    def _api_guard(self) -> bool:
        if self._reject_non_loopback_host():
            return False
        return True

    def _require_session(self) -> str:
        header = self.headers.get('Authorization') or ''
        if not header.startswith('Bearer '):
            raise RemoteApiError(401, 'auth_failed')
        token = header[len('Bearer '):].strip()
        try:
            return self.remote.authenticator.validate_session(token)
        except RemoteAuthError as error:
            raise RemoteApiError(401, 'auth_failed') from error

    def _bearer_token(self) -> str:
        header = self.headers.get('Authorization') or ''
        if not header.startswith('Bearer '):
            raise RemoteApiError(401, 'auth_failed')
        return header[len('Bearer '):].strip()

    def _run(self, action: Callable[[], tuple[int, dict[str, Any]]]) -> None:
        try:
            status, payload = action()
        except RemoteApiError as error:
            self._send_error(error.status, error.code)
            return
        except Exception:
            self._send_error(500, 'internal_error')
            return
        self._send_json(payload, status)

    # -- HTTP methods ------------------------------------------------------

    def do_GET(self):  # noqa: N802
        if not self._api_guard():
            return
        path = self.path.split('?', 1)[0]
        if path == '/':
            page = (
                '<!doctype html><html><head><meta name="referrer" '
                'content="no-referrer"><title>AI-Work Remote</title></head>'
                '<body><p>AI-Work Remote service (loopback only).</p></body>'
                '</html>'
            )
            data = page.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == '/health':
            def action():
                device_id = self._require_session()
                self.remote._check_rate_limits(device_id, 'health_read')
                return self.remote._command_health_read(device_id, '', {})
            self._run(action)
            return
        if path == '/local/devices':
            self._run(self.remote.list_local_devices)
            return
        self._send_error(404, 'not_found')

    def do_POST(self):  # noqa: N802
        if not self._api_guard():
            return
        path = self.path.split('?', 1)[0]
        if path == '/session':
            def action():
                body = self._read_body()
                return self.remote.handle_session(
                    self.headers.get('X-Remote-Device') or '',
                    self.headers.get('X-Remote-Nonce') or '',
                    self.headers.get('X-Remote-Timestamp') or '',
                    self.headers.get('X-Signature') or '',
                    body,
                )
            self._run(action)
            return
        if path == '/pairing/claim':
            def action():
                body = self._read_body()
                source_key = self.client_address[0]
                return self.remote.handle_pairing_claim(body, source_key)
            self._run(action)
            return
        if path == '/local/pairing/start':
            self._run(self.remote.handle_pairing_start)
            return
        if path.startswith('/local/devices/') and path.endswith('/revoke'):
            def action():
                device_id = path[len('/local/devices/'):-len('/revoke')]
                if not device_id or '/' in device_id:
                    raise RemoteApiError(404, 'not_found')
                if not self.remote._revoke_device(device_id):
                    raise RemoteApiError(404, 'unknown_device')
                return 200, {'status': 'REVOKED'}
            self._run(action)
            return
        if path == '/local/devices/revoke-all':
            self._run(self.remote.revoke_all_devices)
            return
        if path == '/command':
            def action():
                body = self._read_body()
                return self.remote.handle_command(
                    body,
                    device_id=self.headers.get('X-Remote-Device') or '',
                    nonce=self.headers.get('X-Remote-Nonce') or '',
                    timestamp=self.headers.get('X-Remote-Timestamp') or '',
                    signature=self.headers.get('X-Signature') or '',
                    session_token=self._bearer_token(),
                )
            self._run(action)
            return
        self._send_error(404, 'not_found')
