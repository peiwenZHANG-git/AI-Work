"""Fixed-allowlist audit events for the remote component."""

from __future__ import annotations

from typing import Callable

from ..health_events import record_health_event
from .auth import audit_device_hash


CODE_OUTCOMES = {
    'auth_failed': 'error',
    'rate_limited': 'warning',
    'command_denied': 'warning',
    'session_created': 'success',
    'pairing_started': 'success',
    'pairing_completed': 'success',
    'pairing_failed': 'error',
    'task_staged': 'success',
    'task_cancelled': 'success',
    'task_confirmed_local': 'success',
    'task_expired': 'warning',
    'device_revoked': 'warning',
    'all_devices_revoked': 'warning',
    'session_revoked': 'success',
}


def record_remote_event(
    code: str,
    *,
    outcome: str | None = None,
    device_id: str | None = None,
    pepper: str | None = None,
    recorder: Callable[..., bool] = record_health_event,
) -> bool:
    """Record one allowlisted remote event; device ids are hashed first."""
    resolved_outcome = outcome or CODE_OUTCOMES.get(code, 'warning')
    device_hash = None
    if device_id and pepper:
        device_hash = audit_device_hash(device_id, pepper)
    try:
        return recorder(
            'remote', resolved_outcome, f'remote_{code}', device=device_hash,
        )
    except Exception:
        return False
