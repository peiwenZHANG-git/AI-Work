"""LAN pairing requires a pending request and explicit local approval."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from typing import Callable

from .auth import RemoteAuthenticator


PENDING_PAIRING_TTL_SECONDS = 600.0


class PairingError(Exception):
    """Stable pairing failure without caller-controlled detail."""


@dataclass
class _PendingRequest:
    request_id: str
    code: str
    device_name: str
    source_ip: str
    client_nonce: str
    claim_token: str
    expires_mono: float
    approved: bool = False
    denied: bool = False
    credential: tuple[str, str] | None = None


class PendingPairingManager:
    """No LAN request can enroll a device before local approval."""

    def __init__(
        self,
        authenticator: RemoteAuthenticator,
        *,
        ttl_seconds: float = PENDING_PAIRING_TTL_SECONDS,
        now_factory: Callable[[], float] | None = None,
        audit: Callable[..., None] | None = None,
    ) -> None:
        self._authenticator = authenticator
        self._now = now_factory or authenticator._now
        self._audit = audit or (lambda *args, **kwargs: None)
        self._ttl = float(ttl_seconds)
        self._lock = threading.Lock()
        self._request: _PendingRequest | None = None

    def start_local(self) -> dict[str, str]:
        code = self._authenticator.start_pairing()
        with self._lock:
            self._request = None
        self._audit('pairing_started')
        return {'pairing_code': code}

    def create_pending(
        self, code: str, device_name: str, source_ip: str,
    ) -> dict[str, str]:
        current = self._now()
        normalized_name = ' '.join(str(device_name or '').split())[:64] or 'unnamed device'
        with self._lock:
            self._expire_locked(current)
            if self._request is not None:
                raise PairingError('pairing_pending_exists')
            active = self._authenticator.active_pairing()
            if not active or not secrets.compare_digest(
                active[0], str(code).strip().upper(),
            ):
                raise PairingError('pairing_invalid')
            if current > active[1]:
                raise PairingError('pairing_expired')
            request = _PendingRequest(
                request_id=secrets.token_hex(16),
                code=str(code).strip().upper(),
                device_name=normalized_name,
                source_ip=str(source_ip),
                client_nonce=secrets.token_urlsafe(32),
                claim_token=secrets.token_urlsafe(32),
                expires_mono=current + self._ttl,
            )
            self._request = request
        self._audit('pairing_pending')
        return {
            'request_id': request.request_id,
            'claim_token': request.claim_token,
            'client_nonce': request.client_nonce,
        }

    def list_pending(self) -> list[dict[str, str]]:
        with self._lock:
            self._expire_locked(self._now())
            if self._request is None or self._request.approved:
                return []
            return [{
                'request_id': self._request.request_id,
                'device_name': self._request.device_name,
                'source_ip': self._request.source_ip,
            }]

    def approve(self, request_id: str) -> bool:
        with self._lock:
            self._expire_locked(self._now())
            request = self._request
            if request is None or request.request_id != request_id or request.denied:
                return False
            if request.approved:
                return True
            try:
                credential = self._authenticator.claim_pairing(
                    request.code, request.device_name,
                )
            except Exception:
                self._request = None
                failed = True
            else:
                request.approved = True
                request.credential = credential
                failed = False
        if failed:
            self._audit('pairing_failed')
            raise PairingError('pairing_failed')
        self._audit('pairing_completed')
        return True

    def deny(self, request_id: str) -> bool:
        with self._lock:
            self._expire_locked(self._now())
            request = self._request
            if request is None or request.request_id != request_id:
                return False
            request.denied = True
            self._request = None
            credential = request.credential
        if credential is not None:
            self._authenticator.revoke_device(credential[0])
        self._audit('pairing_denied')
        return True

    def complete(
        self, request_id: str, claim_token: str, client_nonce: str,
    ) -> dict[str, str]:
        with self._lock:
            self._expire_locked(self._now())
            request = self._request
            if (
                request is None
                or request.request_id != request_id
                or not request.approved
                or request.credential is None
                or not secrets.compare_digest(request.claim_token, claim_token)
                or not secrets.compare_digest(request.client_nonce, client_nonce)
            ):
                raise PairingError('pairing_claim_invalid')
            device_id, secret = request.credential
            self._request = None
        return {'device_id': device_id, 'secret': secret}

    def clear(self) -> None:
        with self._lock:
            credential = self._request.credential if self._request else None
            self._request = None
        if credential is not None:
            self._authenticator.revoke_device(credential[0])
        self._authenticator.clear_pairing()

    def _expire_locked(self, current: float) -> None:
        if self._request is not None and current > self._request.expires_mono:
            credential = self._request.credential
            self._request = None
            if credential is not None:
                self._authenticator.revoke_device(credential[0])
