"""Remote authentication primitives (Phase 3B-1, no networking).

Covers pairing codes, per-device Credential Manager secrets, HMAC request
signatures with nonce/timestamp replay protection, and in-memory sessions.
Secrets never appear in URLs, logs, or return values.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from ..mail_backends import WindowsCredentialManagerSecretStore


REMOTE_CREDENTIAL_SERVICE = 'AI-Work/windows-gui/remote'
PAIRING_CODE_LENGTH = 8
PAIRING_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
PAIRING_CODE_TTL_SECONDS = 5 * 60
SESSION_IDLE_TTL_SECONDS = 15 * 60
SESSION_ABSOLUTE_TTL_SECONDS = 12 * 60 * 60
MAX_SESSIONS_PER_DEVICE = 2
TIMESTAMP_WINDOW_SECONDS = 90
NONCE_CACHE_CAPACITY = 4096
_DEVICE_ID_HEX_CHARS = 16
_SECRET_TOKEN_BYTES = 32


class RemoteAuthError(Exception):
    """Deterministic remote authentication failure without sensitive detail."""


class PairingCodeInvalidError(RemoteAuthError):
    """Raised when a pairing code is wrong, used, or unknown."""


class PairingCodeExpiredError(RemoteAuthError):
    """Raised when a pairing code exists but its TTL has elapsed."""


class UnknownDeviceError(RemoteAuthError):
    """Raised when the device id is not enrolled."""


class TimestampError(RemoteAuthError):
    """Raised when the request timestamp is malformed or outside the window."""


class ReplayError(RemoteAuthError):
    """Raised when a nonce has already been used."""


class SignatureError(RemoteAuthError):
    """Raised when the HMAC signature does not match."""


class SessionError(RemoteAuthError):
    """Raised when a session token is unknown or expired."""


def _default_secret_store(service: str, username: str) -> Any:
    return WindowsCredentialManagerSecretStore(service, username)


def body_fingerprint(body: bytes | None) -> str:
    """Return the SHA-256 hex digest that request signatures bind to."""
    return hashlib.sha256(body or b'').hexdigest()


def canonical_signing_string(
    method: str, path: str, body_hash: str, nonce: str, timestamp: int,
    *, device_id: str | None = None,
) -> str:
    fields = [
        method.strip().upper(),
        path,
        body_hash,
        nonce,
        str(int(timestamp)),
    ]
    if device_id is not None:
        fields.append(f'device:{device_id}')
    return '\n'.join(fields)


def sign_request(
    secret: str, method: str, path: str, body_hash: str, nonce: str,
    timestamp: int,
    *, device_id: str | None = None,
) -> str:
    message = canonical_signing_string(
        method, path, body_hash, nonce, timestamp, device_id=device_id,
    ).encode('utf-8')
    return hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()


def audit_device_hash(device_id: str, pepper: str) -> str:
    """Return a stable opaque, non-reversible device reference for logs."""
    digest = hmac.new(
        pepper.encode('utf-8'),
        f'device:{device_id}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return digest[:16]


@dataclass
class DeviceRecord:
    device_id: str
    name: str
    created_mono: float
    status: str = 'active'
    created_at_iso: str = ''


@dataclass
class _Session:
    device_id: str
    created_mono: float
    last_used_mono: float


class DeviceSecretStore:
    """Device secrets in Credential Manager; injectable for tests."""

    def __init__(
        self,
        store_factory: Callable[[str, str], Any] = _default_secret_store,
    ) -> None:
        self._factory = store_factory

    def _username(self, device_id: str) -> str:
        return f'device_{device_id}'

    def set_secret(self, device_id: str, secret: str) -> None:
        self._factory(
            REMOTE_CREDENTIAL_SERVICE, self._username(device_id)
        ).set_secret(secret)

    def get_secret(self, device_id: str) -> str | None:
        return self._factory(
            REMOTE_CREDENTIAL_SERVICE, self._username(device_id)
        ).get_secret()

    def delete_secret(self, device_id: str) -> bool:
        store = self._factory(REMOTE_CREDENTIAL_SERVICE, self._username(device_id))
        if not hasattr(store, 'delete_secret'):
            return False
        return bool(store.delete_secret())


class RemoteAuthenticator:
    """Thread-safe pairing, device, signature, and session state."""

    def __init__(
        self,
        *,
        secret_store: DeviceSecretStore | None = None,
        now_factory: Callable[[], float] = time.monotonic,
        wall_factory: Callable[[], float] = time.time,
    ) -> None:
        self._secret_store = secret_store or DeviceSecretStore()
        self._now = now_factory
        self._wall = wall_factory
        self._lock = threading.Lock()
        self._pairing_code: str | None = None
        self._pairing_expires_mono = 0.0
        self._pairing_used = False
        self._devices: dict[str, DeviceRecord] = {}
        self._nonces: OrderedDict[str, float] = OrderedDict()
        self._sessions: dict[str, _Session] = {}

    # -- pairing -----------------------------------------------------

    def start_pairing(self, *, now: float | None = None) -> str:
        current = self._now() if now is None else float(now)
        code = ''.join(
            secrets.choice(PAIRING_CODE_ALPHABET)
            for _ in range(PAIRING_CODE_LENGTH)
        )
        with self._lock:
            self._pairing_code = code
            self._pairing_expires_mono = current + PAIRING_CODE_TTL_SECONDS
            self._pairing_used = False
        return code

    def _verify_pairing_code_locked(self, code: str) -> None:
        if (
            not self._pairing_code
            or self._pairing_used
            or not secrets.compare_digest(self._pairing_code, code)
        ):
            raise PairingCodeInvalidError('pairing code is invalid or already used')
        if self._now() > self._pairing_expires_mono:
            raise PairingCodeExpiredError('pairing code has expired')

    def clear_pairing(self) -> None:
        """Drop any pending pairing code (restart semantics)."""
        with self._lock:
            self._pairing_code = None
            self._pairing_used = False
            self._pairing_expires_mono = 0.0

    def claim_pairing(
        self, code: str, device_name: str, *, now: float | None = None,
    ) -> tuple[str, str]:
        code = str(code).strip().upper()
        name = ' '.join(str(device_name or '').split())[:64]
        if not name:
            name = 'unnamed device'
        current = self._now() if now is None else float(now)
        with self._lock:
            self._verify_pairing_code_locked(code)
            self._pairing_used = True
            device_id = secrets.token_hex(_DEVICE_ID_HEX_CHARS // 2)
            while device_id in self._devices:
                device_id = secrets.token_hex(_DEVICE_ID_HEX_CHARS // 2)
            device_secret = secrets.token_urlsafe(_SECRET_TOKEN_BYTES)
            self._secret_store.set_secret(device_id, device_secret)
            self._devices[device_id] = DeviceRecord(
                device_id=device_id, name=name, created_mono=current,
                created_at_iso=datetime.fromtimestamp(
                    self._wall()
                ).astimezone().isoformat(),
            )
            return device_id, device_secret

    def restore_devices(self, records: list[dict[str, Any]]) -> int:
        """Restore persisted device metadata; returns restored count.

        Only records with valid opaque ids and known statuses are accepted;
        anything else is skipped so a corrupt registry cannot escalate.
        """
        current = self._now()
        restored = 0
        with self._lock:
            for record in records:
                if not isinstance(record, dict):
                    continue
                device_id = record.get('device_id')
                name = record.get('name')
                status = record.get('status')
                created_at_iso = record.get('created_at')
                if (
                    not isinstance(device_id, str)
                    or len(device_id) != _DEVICE_ID_HEX_CHARS
                    or any(char not in '0123456789abcdef' for char in device_id)
                    or device_id in self._devices
                ):
                    continue
                if not isinstance(name, str) or not name or len(name) > 64:
                    name = 'unnamed device'
                if status not in ('active', 'revoked'):
                    continue
                if not isinstance(created_at_iso, str):
                    created_at_iso = ''
                self._devices[device_id] = DeviceRecord(
                    device_id=device_id,
                    name=name,
                    created_mono=current,
                    status=status,
                    created_at_iso=created_at_iso,
                )
                restored += 1
        return restored

    def list_devices(self) -> list[DeviceRecord]:
        with self._lock:
            return list(self._devices.values())

    def revoke_device(self, device_id: str) -> bool:
        """Revoke one device: kill sessions and remove its stored secret."""
        with self._lock:
            device = self._devices.get(device_id)
            if device is None or device.status != 'active':
                return False
            device.status = 'revoked'
            dead_tokens = [
                token for token, session in self._sessions.items()
                if session.device_id == device_id
            ]
            for token in dead_tokens:
                self._sessions.pop(token, None)
        self._secret_store.delete_secret(device_id)
        return True

    def revoke_all_devices(self) -> int:
        """Revoke every active device and clear all replay state."""
        with self._lock:
            active = [
                device for device in self._devices.values()
                if device.status == 'active'
            ]
            for device in active:
                device.status = 'revoked'
            self._sessions.clear()
            self._nonces.clear()
        for device in active:
            self._secret_store.delete_secret(device.device_id)
        return len(active)

    def clear_nonces(self) -> None:
        with self._lock:
            self._nonces.clear()

    # -- request authentication ---------------------------------------

    def authenticate(
        self,
        device_id: str,
        nonce: str,
        timestamp: int,
        signature: str,
        method: str,
        path: str,
        body: bytes | None,
        *,
        signed_device_id: str | None = None,
    ) -> None:
        device_id = str(device_id)
        nonce = str(nonce)
        current = self._now()
        wall_now = self._wall()
        with self._lock:
            device = self._devices.get(device_id)
            if device is None or device.status != 'active':
                raise UnknownDeviceError('device is not enrolled')
            if nonce in self._nonces:
                raise ReplayError('request nonce has already been used')
            try:
                stamp = int(timestamp)
            except (TypeError, ValueError) as error:
                raise TimestampError('request timestamp is malformed') from error
            if abs(wall_now - stamp) > TIMESTAMP_WINDOW_SECONDS:
                raise TimestampError('request timestamp is outside the allowed window')
            secret = self._secret_store.get_secret(device_id)
            if not secret:
                raise UnknownDeviceError('device credential is unavailable')
            body_hash = body_fingerprint(body)
            expected = sign_request(
                secret, str(method), str(path), body_hash, nonce, stamp,
                device_id=signed_device_id,
            )
            if not hmac.compare_digest(expected, str(signature)):
                raise SignatureError('request signature does not match')
            self._nonces[nonce] = current + TIMESTAMP_WINDOW_SECONDS
            while len(self._nonces) > NONCE_CACHE_CAPACITY:
                self._nonces.popitem(last=False)
            self._expire_nonces_locked(current)

    def _expire_nonces_locked(self, current: float) -> None:
        expired = [
            nonce for nonce, expires in self._nonces.items() if expires <= current
        ]
        for nonce in expired:
            self._nonces.pop(nonce, None)

    # -- sessions ------------------------------------------------------

    def create_session(self, device_id: str, *, now: float | None = None) -> str:
        current = self._now() if now is None else float(now)
        token = secrets.token_urlsafe(32)
        with self._lock:
            device = self._devices.get(device_id)
            if device is None or device.status != 'active':
                raise UnknownDeviceError('device is not enrolled')
            device_sessions = sorted(
                (
                    (candidate_token, session)
                    for candidate_token, session in self._sessions.items()
                    if session.device_id == device_id
                ),
                key=lambda item: item[1].last_used_mono,
            )
            while len(device_sessions) >= MAX_SESSIONS_PER_DEVICE:
                oldest_token, _ = device_sessions.pop(0)
                self._sessions.pop(oldest_token, None)
            self._sessions[token] = _Session(
                device_id=device_id,
                created_mono=current,
                last_used_mono=current,
            )
            return token

    def validate_session(self, token: str, *, now: float | None = None) -> str:
        current = self._now() if now is None else float(now)
        with self._lock:
            session = self._sessions.get(str(token))
            if session is None:
                raise SessionError('session is unknown')
            if current - session.last_used_mono > SESSION_IDLE_TTL_SECONDS:
                self._sessions.pop(str(token), None)
                raise SessionError('session has expired')
            if current - session.created_mono > SESSION_ABSOLUTE_TTL_SECONDS:
                self._sessions.pop(str(token), None)
                raise SessionError('session has expired')
            session.last_used_mono = current
            return session.device_id

    def revoke_session(self, token: str) -> bool:
        with self._lock:
            return self._sessions.pop(str(token), None) is not None

    def revoke_device_sessions(self, device_id: str) -> int:
        with self._lock:
            dead_tokens = [
                token for token, session in self._sessions.items()
                if session.device_id == device_id
            ]
            for token in dead_tokens:
                self._sessions.pop(token, None)
            return len(dead_tokens)
