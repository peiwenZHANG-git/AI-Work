"""Fixed-allowlist audit events for the remote component."""

from __future__ import annotations

import hashlib
import hmac

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
    'task_local_rejected': 'warning',
    'task_execution_succeeded': 'success',
    'task_execution_failed': 'error',
}


def audit_task_hash(task_id: str, pepper: str) -> str:
    """Return a stable opaque, non-reversible task reference for logs.

    Same security principles as the device hash: irreversible, peppered,
    fixed-length output; scoped separately from device identities.
    """
    digest = hmac.new(
        pepper.encode('utf-8'),
        f'task:{task_id}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return digest[:16]


def record_remote_event(
    code: str,
    *,
    outcome: str | None = None,
    device_id: str | None = None,
    task_id: str | None = None,
    pepper: str | None = None,
    recorder: Callable[..., bool] = record_health_event,
) -> bool:
    """Record one allowlisted remote event; ids are hashed first."""
    resolved_outcome = outcome or CODE_OUTCOMES.get(code, 'warning')
    device_hash = None
    if device_id and pepper:
        device_hash = audit_device_hash(device_id, pepper)
    task_hash = None
    if task_id and pepper:
        task_hash = audit_task_hash(task_id, pepper)
    try:
        return recorder(
            'remote', resolved_outcome, f'remote_{code}',
            device=device_hash, task=task_hash,
        )
    except Exception:
        return False
