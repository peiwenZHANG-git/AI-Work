"""Pinned-TLS REST client for the opt-in Remote LAN transport."""

from __future__ import annotations

import http.client
import json
import secrets
import ssl
import time
from typing import Any, Callable

from .auth import body_fingerprint, sign_request
from .tls import client_pinned_spki


class RemoteClientError(Exception):
    """Stable client failure; no secret or server diagnostic is included."""


class RemoteClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        spki_sha256: str,
        timeout: float = 5.0,
        wall_factory: Callable[[], float] = time.time,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.expected_spki = str(spki_sha256).casefold()
        self.timeout = float(timeout)
        self._wall = wall_factory
        self.device_id = ''
        self.device_secret = ''
        self.session = ''

    def _context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b'',
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        if '?' in path or '#' in path:
            raise RemoteClientError('remote path invalid')
        connection = http.client.HTTPSConnection(
            self.host, self.port, timeout=self.timeout, context=self._context(),
        )
        try:
            connection.connect()
            der_certificate = connection.sock.getpeercert(binary_form=True)
            if not der_certificate:
                raise RemoteClientError('server certificate unavailable')
            client_pinned_spki(der_certificate, self.expected_spki)
            connection.request(
                method, path, body=body, headers=headers or {},
            )
            response = connection.getresponse()
            return response.status, response.read()
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            raise RemoteClientError('remote transport failed') from error
        finally:
            connection.close()

    def _signed_headers(
        self, method: str, path: str, body: bytes, *,
        device_id: str, secret: str, bind_device: bool,
    ) -> dict[str, str]:
        nonce = secrets.token_urlsafe(24)
        stamp = int(self._wall())
        signature = sign_request(
            secret, method, path, body_fingerprint(body), nonce, stamp,
            device_id=device_id if bind_device else None,
        )
        return {
            'Host': f'{self.host}:{self.port}',
            'X-Remote-Device': device_id,
            'X-Remote-Nonce': nonce,
            'X-Remote-Timestamp': str(stamp),
            'X-Signature': signature,
        }

    def open_session(self, *, device_id: str, secret: str) -> str:
        self.device_id = device_id
        self.device_secret = secret
        headers = self._signed_headers(
            'POST', '/session', b'{}', device_id=device_id, secret=secret,
            bind_device=False,
        )
        headers['Content-Type'] = 'application/json'
        status, data = self._request('POST', '/session', body=b'{}', headers=headers)
        if status != 200:
            raise RemoteClientError('remote session failed')
        try:
            self.session = str(json.loads(data)['session'])
        except (ValueError, KeyError, TypeError) as error:
            raise RemoteClientError('remote session response invalid') from error
        return self.session

    def health(self) -> dict[str, Any]:
        return self.command('health.read')

    def command(self, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.session:
            raise RemoteClientError('remote session is not open')
        payload = {
            'command': command,
            'request_id': secrets.token_urlsafe(24),
            'params': params or {},
        }
        body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        headers = self._signed_headers(
            'POST', '/command', body, device_id=self.device_id,
            secret=self.device_secret, bind_device=True,
        )
        headers.update({
            'Authorization': f'Bearer {self.session}',
            'Content-Type': 'application/json',
        })
        status, data = self._request('POST', '/command', body=body, headers=headers)
        if status != 200:
            raise RemoteClientError('remote command failed')
        try:
            result = json.loads(data)
        except ValueError as error:
            raise RemoteClientError('remote response invalid') from error
        if not isinstance(result, dict):
            raise RemoteClientError('remote response invalid')
        return result

    def pairing_pending(
        self, *, pairing_code: str, device_name: str,
    ) -> dict[str, str]:
        body = json.dumps({
            'code': pairing_code, 'device_name': device_name,
        }, separators=(',', ':')).encode('utf-8')
        status, data = self._request(
            'POST', '/pairing/pending', body=body,
            headers={
                'Host': f'{self.host}:{self.port}',
                'Content-Type': 'application/json',
            },
        )
        if status != 200:
            raise RemoteClientError('pairing request failed')
        try:
            result = json.loads(data)
        except ValueError as error:
            raise RemoteClientError('pairing response invalid') from error
        if not isinstance(result, dict):
            raise RemoteClientError('pairing response invalid')
        return result

    def pairing_complete(
        self, *, request_id: str, claim_token: str, client_nonce: str,
        attempts: int = 20, interval_seconds: float = 1.0,
    ) -> dict[str, str]:
        body = json.dumps({
            'request_id': request_id,
            'claim_token': claim_token,
            'client_nonce': client_nonce,
        }, separators=(',', ':')).encode('utf-8')
        headers = {
            'Host': f'{self.host}:{self.port}',
            'Content-Type': 'application/json',
        }
        for _ in range(attempts):
            status, data = self._request(
                'POST', '/pairing/complete', body=body, headers=headers,
            )
            if status == 200:
                try:
                    result = json.loads(data)
                except ValueError as error:
                    raise RemoteClientError('pairing response invalid') from error
                if not isinstance(result, dict):
                    raise RemoteClientError('pairing response invalid')
                return result
            time.sleep(interval_seconds)
        raise RemoteClientError('local pairing approval was not completed')
