"""Remote command protocol primitives (Phase 3B-1, no networking).

Fixed command enum, strict request parsing, and the idempotency cache that
keeps duplicate stage requests from creating duplicate side effects.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable


MAX_BODY_BYTES = 256 * 1024
REQUEST_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{16,128}$')
_EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$')
_TEXT_FORBIDDEN = ('\r', '\n', '\x00')


class ProtocolError(Exception):
    """Deterministic protocol failure without sensitive detail."""


class UnknownCommandError(ProtocolError):
    """Raised when the command name is not in the fixed allowlist."""


class InvalidRequestError(ProtocolError):
    """Raised for malformed envelopes, ids, or parameters."""


@dataclass(frozen=True)
class CommandSpec:
    name: str
    level: int
    mutating: bool
    stages_task: bool
    requires_local_confirmation: bool
    rate_limit: str
    audit: bool


COMMANDS: dict[str, CommandSpec] = {
    'health.read': CommandSpec(
        'health.read', 0, False, False, False, 'health_read', False,
    ),
    'task.status': CommandSpec(
        'task.status', 0, False, False, False, 'task_status', False,
    ),
    'task.cancel': CommandSpec(
        'task.cancel', 0, True, False, False, 'task_cancel', True,
    ),
    'browser.request_click': CommandSpec(
        'browser.request_click', 2, True, True, True, 'mutating_stage', True,
    ),
    'browser.request_download': CommandSpec(
        'browser.request_download', 2, True, True, True, 'mutating_stage', True,
    ),
    'mail.request_draft': CommandSpec(
        'mail.request_draft', 2, True, True, True, 'mutating_stage', True,
    ),
    'session.revoke_self': CommandSpec(
        'session.revoke_self', 0, True, False, False, 'session', True,
    ),
}


def _require_text_param(
    params: dict[str, Any], name: str, *, min_length: int, max_length: int,
) -> str:
    value = params.get(name)
    if not isinstance(value, str):
        raise InvalidRequestError(f'{name} must be a string')
    if not min_length <= len(value) <= max_length:
        raise InvalidRequestError(f'{name} length must be {min_length}..{max_length}')
    if any(char in value for char in _TEXT_FORBIDDEN):
        raise InvalidRequestError(f'{name} must not contain control characters')
    return value


def _validate_click_params(params: dict[str, Any]) -> dict[str, Any]:
    text = _require_text_param(params, 'text', min_length=1, max_length=500)
    exact = params.get('exact', True)
    if not isinstance(exact, bool):
        raise InvalidRequestError('exact must be a boolean')
    return {'text': text, 'exact': exact}


def _validate_download_params(params: dict[str, Any]) -> dict[str, Any]:
    validated = _validate_click_params(params)
    filename = params.get('filename', '')
    if not isinstance(filename, str) or len(filename) > 240:
        raise InvalidRequestError('filename length must be at most 240')
    if any(char in filename for char in _TEXT_FORBIDDEN):
        raise InvalidRequestError('filename must not contain control characters')
    validated['filename'] = filename
    return validated


def _validate_draft_params(params: dict[str, Any]) -> dict[str, Any]:
    mailbox_id = params.get('mailbox_id')
    if mailbox_id not in ('master_mail', 'bachelor_mail', 'qq_mail'):
        raise InvalidRequestError('mailbox_id is not allowed')
    to = _require_text_param(params, 'to', min_length=3, max_length=320)
    if not _EMAIL_PATTERN.fullmatch(to):
        raise InvalidRequestError('to must contain one plain email address')
    subject = _require_text_param(params, 'subject', min_length=1, max_length=200)
    body = _require_text_param(params, 'body', min_length=1, max_length=50_000)
    return {
        'mailbox_id': mailbox_id, 'to': to, 'subject': subject, 'body': body,
    }


def _validate_task_cancel_params(params: dict[str, Any]) -> dict[str, Any]:
    task_id = _require_text_param(params, 'task_id', min_length=8, max_length=128)
    return {'task_id': task_id}


_PARAM_VALIDATORS = {
    'browser.request_click': _validate_click_params,
    'browser.request_download': _validate_download_params,
    'mail.request_draft': _validate_draft_params,
    'task.cancel': _validate_task_cancel_params,
}


def validate_command_params(command: str, params: Any) -> dict[str, Any]:
    """Validate one command's parameters; empty dict for parameterless commands."""
    if command not in COMMANDS:
        raise UnknownCommandError('command is not allowlisted')
    if not isinstance(params, dict):
        raise InvalidRequestError('params must be an object')
    validator = _PARAM_VALIDATORS.get(command)
    if validator is None:
        if params:
            raise InvalidRequestError('command does not accept parameters')
        return {}
    return validator(params)


def parse_request_envelope(payload: bytes | str) -> tuple[CommandSpec, str, dict[str, Any]]:
    """Parse and validate {command, request_id, params}; nothing else."""
    if isinstance(payload, bytes):
        if len(payload) > MAX_BODY_BYTES:
            raise InvalidRequestError('request body is too large')
        import json

        try:
            text = payload.decode('utf-8')
        except UnicodeDecodeError as error:
            raise InvalidRequestError('request body is not valid UTF-8') from error
        try:
            envelope = json.loads(text)
        except ValueError as error:
            raise InvalidRequestError('request body is not valid JSON') from error
    else:
        envelope = payload
    if not isinstance(envelope, dict):
        raise InvalidRequestError('request must be a JSON object')
    if set(envelope) - {'command', 'request_id', 'params'}:
        raise InvalidRequestError('request has unsupported fields')
    command = envelope.get('command')
    if not isinstance(command, str) or command not in COMMANDS:
        raise UnknownCommandError('command is not allowlisted')
    request_id = envelope.get('request_id')
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise InvalidRequestError('request_id is malformed')
    params = validate_command_params(command, envelope.get('params', {}))
    return COMMANDS[command], request_id, params


class IdempotencyCache:
    """Bounded (device, request_id) response cache; restart clears it."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 600.0,
        capacity: int = 256,
        now_factory: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._capacity = int(capacity)
        self._now = now_factory
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]] = (
            OrderedDict()
        )

    def get(self, device_id: str, request_id: str) -> dict[str, Any] | None:
        current = self._now()
        key = (str(device_id), str(request_id))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, response = entry
            if expires <= current:
                self._entries.pop(key, None)
                return None
            return dict(response)

    def put(
        self, device_id: str, request_id: str, response: dict[str, Any],
    ) -> None:
        current = self._now()
        key = (str(device_id), str(request_id))
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = (current + self._ttl, dict(response))
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def purge_device(self, device_id: str) -> int:
        """Drop all cached responses for one device; returns purged count."""
        prefix = (str(device_id),)
        with self._lock:
            doomed = [key for key in self._entries if key[:1] == prefix]
            for key in doomed:
                self._entries.pop(key, None)
            return len(doomed)

    def purge_all(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count
