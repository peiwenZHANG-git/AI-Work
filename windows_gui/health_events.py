"""Bounded, non-sensitive JSONL events for the local health dashboard."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


EVENTS_PATH = Path(os.environ.get(
    'LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')
)) / 'AI-Work' / 'health-events.jsonl'
MAX_EVENT_BYTES = 512 * 1024
MAX_ROTATED_FILES = 3
MAX_READ_EVENTS = 200
ALLOWED_COMPONENTS = {
    'mail_digest', 'mail_assistant', 'browser_session', 'mail_cdp', 'mcp',
    'remote',
}
ALLOWED_OUTCOMES = {'success', 'warning', 'error'}
EVENT_SUMMARIES = {
    'digest_completed': 'Daily mail digest completed.',
    'digest_failed': 'Daily mail digest failed.',
    'draft_generated': 'AI mail draft generated.',
    'reply_draft_generated': 'AI reply draft generated.',
    'draft_fallback': 'Local fallback draft generated.',
    'reply_draft_fallback': 'Local fallback reply draft generated.',
    'draft_remote_failed': 'Remote AI draft generation failed.',
    'reply_draft_remote_failed': 'Remote AI reply draft generation failed.',
    'assistant_request_failed': 'Mail assistant request failed.',
    'digest_notification_warning': 'Digest completed but notification was not shown.',
    'worker_recovered': 'Browser session worker auto-recovered.',
    'worker_recovery_failed': 'Browser session worker auto-recovery failed.',
    'remote_auth_failed': 'Remote authentication failed.',
    'remote_rate_limited': 'Remote request was rate limited.',
    'remote_command_denied': 'Remote command was denied.',
    'remote_session_created': 'Remote session was created.',
    'remote_pairing_started': 'Remote pairing was started.',
    'remote_pairing_completed': 'Remote pairing completed.',
    'remote_pairing_failed': 'Remote pairing failed.',
    'remote_pairing_pending': 'A remote pairing request is awaiting local approval.',
    'remote_pairing_denied': 'A remote pairing request was denied locally.',
    'remote_lan_started': 'The Remote LAN listener started.',
    'remote_lan_stopped': 'The Remote LAN listener stopped.',
    'remote_lan_bind_failed': 'The Remote LAN listener failed to start.',
    'remote_lan_network_changed': 'The approved Remote LAN network changed.',
    'remote_task_staged': 'Remote task was staged.',
    'remote_task_cancelled': 'Remote task was cancelled.',
    'remote_task_confirmed_local': 'Remote task was confirmed locally.',
    'remote_task_expired': 'Remote task expired.',
    'remote_device_revoked': 'Remote device was revoked.',
    'remote_all_devices_revoked': 'All remote devices were revoked.',
    'remote_session_revoked': 'Remote session was revoked.',
    'remote_task_rejected_local': 'Remote task was rejected locally.',
    'remote_task_execution_succeeded': 'Remote task executed successfully.',
    'remote_task_execution_failed': 'Remote task execution failed.',
}
_AUDIT_HASH_PATTERN = None  # compiled lazily to keep import time flat
_LOCK = threading.Lock()
_MUTEX_NAME = 'Local\\AI-Work-health-events'


def _validated_hash(value: str | None) -> str | None:
    """Return a validated 16-hex audit hash, or None when absent/invalid."""
    global _AUDIT_HASH_PATTERN
    if value is None:
        return None
    if _AUDIT_HASH_PATTERN is None:
        import re

        _AUDIT_HASH_PATTERN = re.compile(r'^[0-9a-f]{16}$')
    if not isinstance(value, str) or not _AUDIT_HASH_PATTERN.fullmatch(value):
        return ''
    return value


@contextmanager
def _cross_process_lock():
    handle = None
    try:
        import win32api
        import win32event
        import win32con
        handle = win32event.CreateMutex(None, False, _MUTEX_NAME)
        result = win32event.WaitForSingleObject(handle, 5000)
        if result not in (win32con.WAIT_OBJECT_0, getattr(win32con, 'WAIT_ABANDONED', 0x80)):
            raise OSError('health event mutex timeout')
        try:
            yield
        finally:
            win32event.ReleaseMutex(handle)
    except ImportError:
        yield
    finally:
        if handle is not None:
            try:
                win32api.CloseHandle(handle)
            except Exception:
                pass


def _now_iso(now_factory: Callable[[], datetime] | None = None) -> str:
    value = (now_factory or (lambda: datetime.now().astimezone()))()
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.astimezone()
    return value.isoformat()


def _rotate(path: Path, max_bytes: int, rotations: int) -> None:
    try:
        oversized = path.stat().st_size >= max_bytes
    except FileNotFoundError:
        return
    if not oversized:
        return
    for index in range(rotations, 0, -1):
        source = path if index == 1 else Path(f'{path}.{index - 1}')
        target = Path(f'{path}.{index}')
        if source.exists():
            os.replace(source, target)


def record_health_event(
    component: str,
    outcome: str,
    code: str,
    *,
    path: Path = EVENTS_PATH,
    now_factory: Callable[[], datetime] | None = None,
    max_bytes: int = MAX_EVENT_BYTES,
    rotations: int = MAX_ROTATED_FILES,
    device: str | None = None,
    task: str | None = None,
) -> bool:
    """Append an allowlisted event; caller data and exception text are never stored."""
    if component not in ALLOWED_COMPONENTS:
        return False
    if outcome not in ALLOWED_OUTCOMES or code not in EVENT_SUMMARIES:
        return False
    device_hash = _validated_hash(device)
    task_hash = _validated_hash(task)
    if device_hash == '' or task_hash == '':
        return False
    payload = {
        'component': component,
        'outcome': outcome,
        'code': code,
        'summary': EVENT_SUMMARIES[code],
        'time': _now_iso(now_factory),
    }
    if device_hash is not None:
        payload['device'] = device_hash
    if task_hash is not None:
        payload['task'] = task_hash
    try:
        with _LOCK:
            with _cross_process_lock():
                path.parent.mkdir(parents=True, exist_ok=True)
                _rotate(path, max_bytes, rotations)
                with path.open('a', encoding='utf-8', newline='') as handle:
                    handle.write(json.dumps(payload, ensure_ascii=True) + '\n')
        return True
    except Exception:
        return False


def read_health_events(
    path: Path = EVENTS_PATH,
    *,
    limit: int = MAX_READ_EVENTS,
    rotations: int = MAX_ROTATED_FILES,
) -> dict[str, Any]:
    try:
        with _LOCK:
            with _cross_process_lock():
                return _read_health_events_unlocked(
                    path, limit=limit, rotations=rotations
                )
    except Exception:
        return {'events': [], 'invalid_lines': 1}


def _read_health_events_unlocked(
    path: Path,
    *,
    limit: int,
    rotations: int,
) -> dict[str, Any]:
    events: list[dict[str, str]] = []
    invalid_lines = 0
    paths = [Path(f'{path}.{index}') for index in range(rotations, 0, -1)] + [path]
    for candidate in paths:
        try:
            lines = candidate.read_text(encoding='utf-8').splitlines()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            invalid_lines += 1
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                invalid_lines += 1
                continue
            if not isinstance(item, dict):
                invalid_lines += 1
                continue
            if (
                item.get('component') not in ALLOWED_COMPONENTS
                or item.get('outcome') not in ALLOWED_OUTCOMES
                or item.get('code') not in EVENT_SUMMARIES
                or not _has_timezone(str(item.get('time') or ''))
            ):
                invalid_lines += 1
                continue
            event = {
                'component': item['component'],
                'outcome': item['outcome'],
                'code': item['code'],
                'summary': EVENT_SUMMARIES[item['code']],
                'time': str(item.get('time') or ''),
            }
            if item.get('device') is not None:
                event['device'] = str(item['device'])
            if item.get('task') is not None:
                event['task'] = str(item['task'])
            events.append(event)
    return {'events': events[-max(0, min(int(limit), MAX_READ_EVENTS)):], 'invalid_lines': invalid_lines}


def _has_timezone(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


__all__ = ['EVENTS_PATH', 'read_health_events', 'record_health_event']
