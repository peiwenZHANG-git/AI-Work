"""Independent loopback listener for pairing and confirmation actions."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .pairing import PairingError, PendingPairingManager
from .protocol import MAX_BODY_BYTES
from ..task_center import TaskCenterError
from .server import (
    RemoteApiError, RemoteServer, _CONFIRMATION_PAGE_TEMPLATE,
)


DEFAULT_LOCAL_PLANE_PORT = 8934


class _LocalPlaneHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class _LocalPlaneHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, server: 'LocalPlaneServer', **kwargs) -> None:
        self.plane = server
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):  # noqa: N802 - no request logging
        pass

    def _guard(self) -> bool:
        expected = f'127.0.0.1:{self.plane.port}'
        host_valid = (self.headers.get('Host') or '').casefold() == expected
        origin = self.headers.get('Origin')
        origin_valid = not origin or origin == f'http://{expected}'
        if not host_valid or not origin_valid:
            self._send_error(403, 'forbidden')
            return False
        return True

    def _path(self) -> str:
        if '?' in self.path or '#' in self.path:
            raise RemoteApiError(400, 'invalid_request')
        return self.path

    def _json_body(self) -> dict:
        content_type = (
            self.headers.get('Content-Type', '')
            .split(';', 1)[0].strip().casefold()
        )
        if content_type != 'application/json':
            raise RemoteApiError(400, 'invalid_request')
        try:
            length = int(self.headers.get('Content-Length') or '')
        except ValueError as error:
            raise RemoteApiError(400, 'invalid_request') from error
        if length < 0 or length > MAX_BODY_BYTES:
            raise RemoteApiError(413, 'request_too_large')
        try:
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as error:
            raise RemoteApiError(400, 'invalid_request') from error
        if not isinstance(payload, dict):
            raise RemoteApiError(400, 'invalid_request')
        return payload

    def _send_json(self, payload: dict, status: int = 200) -> None:
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

    def _send_html(self, html: str) -> None:
        data = html.encode('utf-8')
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

    def _send_error(self, status: int, code: str) -> None:
        self.close_connection = True
        self._send_json({'error': code}, status)

    def _run(self, action: Callable[[], tuple[int, dict]]) -> None:
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
        if path == '/local/confirmations':
            self._run(self.plane.remote.local_confirmations_view)
        elif path == '/local/confirmations/page':
            self._send_html(_CONFIRMATION_PAGE_TEMPLATE)
        elif path == '/local/devices':
            self._run(self.plane.remote.list_local_devices)
        elif path == '/local/pairing/pending':
            self._run(lambda: (200, {'requests': self.plane.pairing.list_pending()}))
        else:
            self._send_error(404, 'not_found')

    def do_POST(self):  # noqa: N802
        if not self._guard():
            return
        try:
            path = self._path()
        except RemoteApiError as error:
            self._send_error(error.status, error.code)
            return
        if path == '/local/pairing/start':
            def start():
                result = self.plane.pairing.start_local()
                result.update(self.plane.lan_bootstrap())
                return 200, result
            self._run(start)
            return
        if path.startswith('/local/pairing/') and (
            path.endswith('/approve') or path.endswith('/deny')
        ):
            def pairing_action():
                request_id = path[len('/local/pairing/'):-len('approve') - 1]
                approved = path.endswith('/approve')
                changed = (
                    self.plane.pairing.approve(request_id) if approved
                    else self.plane.pairing.deny(request_id)
                )
                if not changed:
                    raise RemoteApiError(404, 'not_found')
                return 200, {'status': 'APPROVED' if approved else 'DENIED'}
            self._run(pairing_action)
            return
        if path == '/local/confirmations/token':
            def token():
                return self.plane.remote.handle_action_token(self._json_body())
            self._run(token)
            return
        if path.startswith('/local/confirmations/') and (
            path.endswith('/approve') or path.endswith('/cancel')
        ):
            def confirm():
                verb = 'approve' if path.endswith('/approve') else 'cancel'
                task_id = path[len('/local/confirmations/'):-len(verb) - 1]
                payload = self._json_body()
                if not self.plane.remote._consume_action_token(
                    str(payload.get('action_token') or ''),
                    task_id=task_id, action=verb,
                ):
                    raise RemoteApiError(403, 'csrf_invalid')
                if verb == 'approve':
                    try:
                        result = self.plane.remote.adapters.approve_task(task_id)
                    except TaskCenterError as error:
                        raise RemoteApiError(409, 'task_not_pending') from error
                    except Exception:
                        self.plane.remote._audit(
                            'task_execution_failed', task_id=task_id,
                        )
                        raise RemoteApiError(500, 'execution_failed')
                    self.plane.remote._audit('task_execution_succeeded', task_id=task_id)
                    return 200, result
                if not self.plane.remote.adapters.reject_task(task_id):
                    raise RemoteApiError(404, 'unknown_task')
                self.plane.remote._audit('task_local_rejected', task_id=task_id)
                return 200, {'status': 'CANCELLED'}
            self._run(confirm)
            return
        if path.startswith('/local/devices/') and path.endswith('/revoke'):
            def revoke():
                device_id = path[len('/local/devices/'):-len('/revoke')]
                if not device_id or '/' in device_id:
                    raise RemoteApiError(404, 'not_found')
                if not self.plane.remote._revoke_device(device_id):
                    raise RemoteApiError(404, 'unknown_device')
                return 200, {'status': 'REVOKED'}
            self._run(revoke)
            return
        if path == '/local/devices/revoke-all':
            self._run(self.plane.remote.revoke_all_devices)
            return
        self._send_error(404, 'not_found')


class LocalPlaneServer:
    """Loopback-only management/confirmation plane for LAN mode."""

    def __init__(
        self,
        *,
        remote: RemoteServer,
        pairing: PendingPairingManager,
        port: int = DEFAULT_LOCAL_PLANE_PORT,
        lan_bootstrap: Callable[[], dict] | None = None,
    ) -> None:
        if port < 0 or port > 65535:
            raise ValueError('invalid local plane port')
        self.remote = remote
        self.pairing = pairing
        self.requested_port = int(port)
        self.port = int(port)
        self._lan_bootstrap = lan_bootstrap or (lambda: {})
        self._httpd: _LocalPlaneHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = lambda *args, **kwargs: _LocalPlaneHandler(
            *args, server=self, **kwargs,
        )
        self._httpd = _LocalPlaneHTTPServer(('127.0.0.1', self.requested_port), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name='remote-local-plane', daemon=True,
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
