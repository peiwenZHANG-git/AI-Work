"""Loopback-only Remote HTTP server (Phase 3B-2, no MCP surface).

Binds 127.0.0.1 only. Every endpoint requires authentication except the
pairing endpoints added in 3B-3. Fixed JSON error codes, no caller-provided
detail in any response.
"""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import threading
from collections import OrderedDict
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ..mail_backends import WindowsCredentialManagerSecretStore
from ..task_center import TaskCenterError
from ..system_health import collect_dashboard_health
from .audit import record_remote_event
from .audit import CODE_OUTCOMES, audit_task_hash
from .adapters import RemoteAdapters
from .auth import (
    REMOTE_CREDENTIAL_SERVICE,
    PairingCodeExpiredError,
    PairingCodeInvalidError,
    RemoteAuthError,
    RemoteAuthenticator,
    audit_device_hash,
)
from .policy import LIMITS, RateLimiter
from .protocol import (
    ProtocolError, RequestIdConflictError, UnknownCommandError,
    parse_request_envelope,
)
from .protocol import IdempotencyCache


DEFAULT_PORT = 8932
MAX_BODY_BYTES = 256 * 1024

_CONFIRMATION_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer">
<title>AI-Work Remote 待确认任务</title></head>
<body>
<h1>Remote 待确认任务</h1>
<p>以下请求由已配对设备发起，需要本机确认后才会执行。</p>
<ul id="confirmations"></ul>
<p id="status"></p>
<script>
async function refresh() {
  const list = document.getElementById('confirmations');
  list.innerHTML = '';
  const response = await fetch('/local/confirmations');
  const payload = await response.json();
  for (const task of payload.confirmations || []) {
    const item = document.createElement('li');
    item.textContent = `${task.action} — ${task.summary}（设备 ${task.device}）`;
    const approve = document.createElement('button');
    approve.textContent = '批准执行';
    approve.addEventListener('click', () => act(task.task_id, 'approve'));
    const reject = document.createElement('button');
    reject.textContent = '拒绝';
    reject.addEventListener('click', () => act(task.task_id, 'cancel'));
    item.appendChild(approve);
    item.appendChild(reject);
    list.appendChild(item);
  }
  if (!(payload.confirmations || []).length) {
    document.getElementById('status').textContent = '当前没有待确认任务。';
  } else {
    document.getElementById('status').textContent = '';
  }
}
async function act(taskId, verb) {
  const tokenResponse = await fetch('/local/confirmations/token');
  const tokenPayload = await tokenResponse.json();
  const response = await fetch(
    `/local/confirmations/${taskId}/${verb}`,
    {method: 'POST', headers: {'X-Local-CSRF': tokenPayload.token || ''}},
  );
  if (!response.ok) {
    document.getElementById('status').textContent =
      '操作失败（' + response.status + '）。';
  }
  refresh();
}
refresh();
</script>
</body></html>
"""


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
        pepper_provider: Callable[[], str] = _default_audit_pepper,
        audit_recorder: Callable[..., bool] = record_remote_event,
        devices_path: Path | None = None,
        adapters: RemoteAdapters | None = None,
        confirmation_token_ttl: float = 600.0,
    ) -> None:
        if host not in ('127.0.0.1', 'localhost'):
            raise ValueError('Remote server binds loopback addresses only')
        self.host = '127.0.0.1'
        self.requested_port = int(port)
        self.port = int(port)
        self.authenticator = authenticator or RemoteAuthenticator()
        self.limiter = limiter or RateLimiter()
        self._collect_health = health_collector
        self._pepper_provider = pepper_provider
        self._audit_recorder = audit_recorder
        self.adapters = adapters or RemoteAdapters(audit=self._task_audit)
        self._csrf_tokens: OrderedDict[str, float] = OrderedDict()
        self._csrf_lock = threading.Lock()
        self._confirmation_token_ttl = float(confirmation_token_ttl)
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

    def _audit(self, code: str, *, device_id: str | None = None,
               task_id: str | None = None) -> None:
        details = {}
        if device_id:
            details['device_id'] = device_id
        if task_id:
            details['task_id'] = task_id
        try:
            self._audit_recorder(
                code,
                outcome=CODE_OUTCOMES.get(code, 'warning'),
                pepper=self._safe_pepper(),
                **details,
            )
        except Exception:
            pass

    def _task_audit(self, code: str, *, device_id: str | None = None,
                    task_id: str | None = None) -> None:
        self._audit(code, device_id=device_id, task_id=task_id)

    def _safe_pepper(self) -> str | None:
        try:
            return self._pepper_provider()
        except Exception:
            return None

    def _new_action_token(self) -> str:
        """Issue one single-use local action token.

        The token authorizes exactly one approve/cancel call on the local
        confirmation plane and is consumed atomically on use. It defends
        against remote devices and cross-site requests; it is not a
        user-presence proof, and a malicious local process in the same
        user session remains a residual risk.
        """
        token = secrets.token_urlsafe(32)
        current = self.authenticator._now()
        with self._csrf_lock:
            self._csrf_tokens[token] = current + self._confirmation_token_ttl
            expired = [
                value for value, expires in self._csrf_tokens.items()
                if expires <= current
            ]
            for value in expired:
                self._csrf_tokens.pop(value, None)
            while len(self._csrf_tokens) > 64:
                self._csrf_tokens.popitem(last=False)
        return token

    def _consume_action_token(self, token: str) -> bool:
        """Atomically consume one action token; replay fails closed."""
        current = self.authenticator._now()
        with self._csrf_lock:
            expired = [
                value for value, expires in self._csrf_tokens.items()
                if expires <= current
            ]
            for value in expired:
                self._csrf_tokens.pop(value, None)
            return self._csrf_tokens.pop(str(token), None) is not None

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
            'task.cancel': self._command_task_cancel,
            'session.revoke_self': self._command_revoke_self,
            'browser.request_click': partial(
                self._command_stage, command='browser.request_click',
            ),
            'browser.request_download': partial(
                self._command_stage, command='browser.request_download',
            ),
            'mail.request_draft': partial(
                self._command_stage, command='mail.request_draft',
            ),
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
        return 200, {'tasks': self.adapters.list_device_tasks(device_id)}

    def _command_task_cancel(
        self, device_id: str, request_id: str, params: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        task_id = str(params.get('task_id') or '')
        if not self.adapters.cancel_device_task(device_id, task_id):
            raise RemoteApiError(404, 'unknown_task')
        return 200, {'status': 'CANCELLED'}

    def _command_stage(
        self, device_id: str, request_id: str, params: dict[str, Any],
        *, command: str,
    ) -> tuple[int, dict[str, Any]]:
        fingerprint = hashlib.sha256(json.dumps(
            {'command': command, 'params': params},
            sort_keys=True, separators=(',', ':'),
        ).encode('utf-8')).hexdigest()
        try:
            result = self.adapters.stage(
                device_id=device_id, request_id=request_id,
                command=command, params=params, fingerprint=fingerprint,
            )
        except RequestIdConflictError as error:
            raise RemoteApiError(409, 'request_id_conflict') from error
        return 200, result

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
            self.adapters.revoke_device_tasks(device_id)
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
        self.adapters.cancel_all_staged()
        self.idempotency.purge_all()
        self._audit('all_devices_revoked')
        self._save_registry()
        return 200, {'revoked': count}

    def local_confirmations_view(self) -> tuple[int, dict[str, Any]]:
        confirmations = self.adapters.local_confirmations()
        for item in confirmations:
            raw_device = str(item.pop('device_id', ''))
            item['device'] = self._device_hash(raw_device) if raw_device else ''
        return 200, {'confirmations': confirmations}

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
        origin = self.headers.get('Origin')
        if origin and origin != f'http://127.0.0.1:{self.remote.port}':
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
        if path == '/local/confirmations':
            self._run(self.remote.local_confirmations_view)
            return
        if path == '/local/confirmations/token':
            self._send_json(
                {'token': self.remote._new_action_token()}
            )
            return
        if path == '/local/confirmations/page':
            self._send_confirmation_page()
            return
        self._send_error(404, 'not_found')

    def _send_confirmation_page(self) -> None:
        data = _CONFIRMATION_PAGE_TEMPLATE.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Cache-Control', 'no-store')
        self.send_header(
            'Content-Security-Policy',
            "default-src 'none'; script-src 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
        if path.startswith('/local/confirmations/') and (
            path.endswith('/approve') or path.endswith('/cancel')
        ):
            def action():
                if not self.remote._consume_action_token(
                    self.headers.get('X-Local-CSRF') or '',
                ):
                    raise RemoteApiError(403, 'csrf_invalid')
                verb = 'approve' if path.endswith('/approve') else 'cancel'
                task_id = path[
                    len('/local/confirmations/'):-len(verb) - 1
                ]
                if verb == 'approve':
                    try:
                        result = self.remote.adapters.approve_task(task_id)
                    except TaskCenterError as error:
                        raise RemoteApiError(
                            409, 'task_not_pending',
                        ) from error
                    except Exception:
                        self.remote._audit(
                            'task_execution_failed', task_id=task_id,
                        )
                        raise RemoteApiError(500, 'execution_failed')
                    self.remote._audit(
                        'task_execution_succeeded', task_id=task_id,
                    )
                    return 200, result
                if not self.remote.adapters.reject_task(task_id):
                    raise RemoteApiError(404, 'unknown_task')
                self.remote._audit('task_local_rejected', task_id=task_id)
                return 200, {'status': 'CANCELLED'}
            self._run(action)
            return
        self._send_error(404, 'not_found')
