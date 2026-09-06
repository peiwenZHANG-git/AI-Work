"""Bounded, privacy-safe activity history for local agent workflows."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .health_events import record_health_event


HISTORY_PATH = Path(os.environ.get(
    'LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')
)) / 'AI-Work' / 'activity-history.jsonl'
MAX_EVENTS = 2000
MAX_AGE_DAYS = 30
MAX_FILE_BYTES = 1024 * 1024
MAX_ROTATED_FILES = 3
MAX_UI_TASKS = 50
VERSION = 1

WORKFLOWS = frozenset({
    'study_workspace', 'file_cleanup', 'course_download',
    'coding_workspace', 'mail_draft',
})
STATUSES = frozenset({
    'STARTED', 'RUNNING', 'WAITING_CONFIRMATION', 'SUCCEEDED', 'FAILED',
    'BLOCKED', 'INTERRUPTED',
})
SUMMARIES = {
    'task_started': '任务已开始。',
    'step_succeeded': '步骤已完成。',
    'confirmation_required': '任务正在等待确认。',
    'task_succeeded': '任务已完成。',
    'task_failed': '任务未完成。',
    'destination_exists': '目标文件已存在，未覆盖。',
    'app_not_installed': '该应用未安装或不在允许列表中。',
    'mail_identity_mismatch': '邮箱身份无法确认，已停止操作。',
    'browser_target_not_found': '页面中没有找到唯一可操作目标。',
    'child_interrupted': '执行进程中断。为避免重复操作，本任务已停止。',
    'history_unavailable': '操作已执行，但活动记录暂时不可用。',
    'clarification_required': '需要补充信息后才能继续。',
    'unsupported_task': '该任务不在当前支持范围内。',
    'plan_changed': '任务计划已变化，原确认已失效。',
    'queue_full': '当前任务较多，请稍后重试。',
}
RESOURCE_KINDS = frozenset({'application', 'file', 'mail', 'browser', 'none'})
_SAFE_VALUE = re.compile(r'^[A-Za-z0-9._ /-]{0,120}$')
_SENSITIVE_MARKER = re.compile(
    r'(?:token|cookie|secret|password|bearer|authorization)', re.IGNORECASE
)
_OPAQUE_ID = re.compile(r'^[0-9a-f]{32}$')
_LOCK = threading.Lock()
_MUTEX_NAME = 'Local\\AI-Work-activity-history'


@contextmanager
def _cross_process_lock():
    handle = None
    try:
        import win32api
        import win32con
        import win32event
        handle = win32event.CreateMutex(None, False, _MUTEX_NAME)
        result = win32event.WaitForSingleObject(handle, 5000)
        if result not in (
            win32con.WAIT_OBJECT_0,
            getattr(win32con, 'WAIT_ABANDONED', 0x80),
        ):
            raise OSError('activity history mutex timeout')
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


def sanitize_resource(kind: str, value: str = '') -> dict[str, str]:
    """Return a fixed, non-sensitive resource descriptor."""
    kind = str(kind)
    if kind not in RESOURCE_KINDS:
        return {'kind': 'none', 'value': ''}
    value = str(value)[:512]
    if kind == 'browser':
        try:
            host = (urlsplit(value).hostname or value).lower().strip('.')
        except ValueError:
            host = ''
        value = host if re.fullmatch(r'[a-z0-9.-]{1,120}', host or '') else ''
    elif kind == 'file':
        normalized = value.replace('\\', '/')
        parts = [part for part in normalized.split('/') if part]
        root = next((p for p in parts if p in {'Downloads', 'Documents'}), '')
        basename = parts[-1] if parts else ''
        value = f'{root}/{basename}' if root and basename != root else root
    elif kind == 'mail':
        mailbox = value.split('/', 1)[0]
        value = mailbox if mailbox in {'master_mail', 'bachelor_mail', 'qq_mail'} else ''
    elif kind == 'application':
        value = value if value in {'vscode', 'notepad', 'calculator', 'explorer', 'edge'} else ''
    else:
        value = ''
    if not _SAFE_VALUE.fullmatch(value) or _SENSITIVE_MARKER.search(value):
        value = ''
    return {'kind': kind, 'value': value}


def _valid_event(item: Any) -> bool:
    return bool(
        isinstance(item, dict)
        and set(item) == {
            'version', 'event_id', 'task_id', 'time', 'workflow', 'step',
            'status', 'code', 'summary', 'resource',
        }
        and item.get('version') == VERSION
        and isinstance(item.get('event_id'), str)
        and _OPAQUE_ID.fullmatch(item['event_id']) is not None
        and isinstance(item.get('task_id'), str)
        and _OPAQUE_ID.fullmatch(item['task_id']) is not None
        and item.get('workflow') in WORKFLOWS
        and item.get('status') in STATUSES
        and item.get('code') in SUMMARIES
        and item.get('summary') == SUMMARIES[item['code']]
        and isinstance(item.get('step'), int)
        and 0 <= item['step'] <= 8
        and isinstance(item.get('resource'), dict)
        and item['resource'] == sanitize_resource(
            item['resource'].get('kind', ''), item['resource'].get('value', '')
        )
    )


def _paths(path: Path, rotations: int) -> list[Path]:
    return [Path(f'{path}.{i}') for i in range(rotations, 0, -1)] + [path]


def _read_all(path: Path, rotations: int) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    invalid = 0
    for candidate in _paths(path, rotations):
        try:
            lines = candidate.read_text(encoding='utf-8').splitlines()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            invalid += 1
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                invalid += 1
                continue
            if _valid_event(item):
                events.append(item)
            else:
                invalid += 1
    return events, invalid


def _event_time(item: dict[str, Any]) -> datetime | None:
    try:
        value = datetime.fromisoformat(item['time'])
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _write_bounded(path: Path, events: list[dict[str, Any]], rotations: int) -> None:
    for candidate in _paths(path, rotations)[:-1]:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
    encoded = [json.dumps(item, ensure_ascii=True) + '\n' for item in events]
    chunks: list[list[str]] = [[]]
    sizes = [0]
    for line in encoded:
        size = len(line.encode('utf-8'))
        if sizes[-1] + size > MAX_FILE_BYTES and chunks[-1]:
            chunks.append([])
            sizes.append(0)
        chunks[-1].append(line)
        sizes[-1] += size
    chunks = chunks[-(rotations + 1):]
    for index, lines in enumerate(chunks):
        destination = path if index == len(chunks) - 1 else Path(
            f'{path}.{len(chunks) - index - 1}'
        )
        temporary = destination.with_suffix(destination.suffix + '.tmp')
        temporary.write_text(''.join(lines), encoding='utf-8')
        os.replace(temporary, destination)


def record_activity(
    task_id: str,
    workflow: str,
    step: int,
    status: str,
    code: str,
    *,
    resource_kind: str = 'none',
    resource_value: str = '',
    path: Path = HISTORY_PATH,
    now_factory: Callable[[], datetime] | None = None,
    id_factory: Callable[[], str] | None = None,
    rotations: int = MAX_ROTATED_FILES,
) -> bool:
    if workflow not in WORKFLOWS or status not in STATUSES or code not in SUMMARIES:
        return False
    if not isinstance(step, int) or not 0 <= step <= 8:
        return False
    now = (now_factory or (lambda: datetime.now().astimezone()))()
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.astimezone()
    event = {
        'version': VERSION,
        'event_id': (id_factory or (lambda: uuid.uuid4().hex))(),
        'task_id': str(task_id),
        'time': now.isoformat(),
        'workflow': workflow,
        'step': step,
        'status': status,
        'code': code,
        'summary': SUMMARIES[code],
        'resource': sanitize_resource(resource_kind, resource_value),
    }
    if not _valid_event(event):
        return False
    try:
        with _LOCK, _cross_process_lock():
            path.parent.mkdir(parents=True, exist_ok=True)
            events, invalid = _read_all(path, rotations)
            cutoff = now.astimezone(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
            events = [item for item in events if (_event_time(item) or cutoff) >= cutoff]
            events = (events + [event])[-MAX_EVENTS:]
            _write_bounded(path, events, rotations)
        if invalid:
            record_health_event('activity_history', 'warning', 'activity_history_corrupt')
        return True
    except Exception:
        return False


def read_activity_history(
    path: Path = HISTORY_PATH,
    *,
    task_limit: int = MAX_UI_TASKS,
    rotations: int = MAX_ROTATED_FILES,
    now_factory: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    try:
        with _LOCK, _cross_process_lock():
            events, invalid = _read_all(path, rotations)
        now = (now_factory or (lambda: datetime.now().astimezone()))()
        cutoff = now.astimezone(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
        events = [item for item in events if (_event_time(item) or cutoff) >= cutoff]
        task_ids: list[str] = []
        for event in reversed(events):
            if event['task_id'] not in task_ids:
                task_ids.append(event['task_id'])
            if len(task_ids) >= max(1, min(int(task_limit), MAX_UI_TASKS)):
                break
        selected = set(task_ids)
        result = [item for item in events if item['task_id'] in selected]
        if invalid:
            record_health_event('activity_history', 'warning', 'activity_history_corrupt')
        return {'events': result, 'invalid_lines': invalid}
    except Exception:
        return {'events': [], 'invalid_lines': 1}


__all__ = [
    'HISTORY_PATH', 'MAX_AGE_DAYS', 'MAX_EVENTS', 'MAX_FILE_BYTES',
    'MAX_ROTATED_FILES', 'MAX_UI_TASKS', 'RESOURCE_KINDS', 'STATUSES',
    'SUMMARIES', 'VERSION', 'WORKFLOWS', 'read_activity_history',
    'record_activity', 'sanitize_resource',
]
