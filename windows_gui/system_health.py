"""Shared, side-effect-free health model for the CLI and local dashboard."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from .browser_mail import _is_loopback_endpoint
from .health_events import EVENTS_PATH, read_health_events
from .imap_mail import (
    BACHELOR_IMAP_CREDENTIAL_SERVICE,
    BACHELOR_IMAP_CREDENTIAL_USERNAME,
    QQ_IMAP_CREDENTIAL_SERVICE,
    QQ_IMAP_CREDENTIAL_USERNAME,
)
from .mail_assistant import (
    ASSISTANT_CREDENTIAL_SERVICE,
    ASSISTANT_DRAFT_CREDENTIAL_USERNAMES,
    BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME,
)
from .mail_backends import WindowsCredentialManagerSecretStore
from .mail_digest import (
    CREDENTIAL_SERVICE,
    DIGEST_DIR,
    MASTER_REFRESH_USERNAME,
    SUMMARY_API_KEY_USERNAME,
)


STATUSES = {'PASS', 'WARN', 'FAIL', 'UNKNOWN'}
ASSISTANT_SERVER_PORT = 8931
SCHEDULED_TASK_NAME = 'AI-Work Daily Mail Digest'
STABLE_TOOL_NAMES = {
    'inspect_path', 'open_path', 'manage_path', 'open_app',
    'click_browser_element',
    'click_control', 'click_menu_item', 'click_mouse', 'click_save_button',
    'download_browser_element', 'download_web_file',
    'double_click', 'drag_mouse', 'focus_and_press', 'focus_window',
    'focus_window_and_hotkey', 'focus_window_and_press',
    'focus_window_and_scroll', 'focus_window_and_type', 'get_mouse_position',
    'hotkey', 'inspect_browser', 'list_controls', 'list_windows', 'move_mouse',
    'navigate_browser', 'open_webpage', 'press_key',
    'right_click', 'screenshot', 'scroll', 'set_save_dialog_filename',
    'type_text', 'open_all_mailboxes', 'summarize_all_mailboxes_today',
    'search_mailboxes', 'create_mail_draft', 'send_mail_draft',
    'start_browser_session', 'stop_browser_session',
}
CREDENTIAL_CHECKS = {
    'qq_imap_summary': (QQ_IMAP_CREDENTIAL_SERVICE, QQ_IMAP_CREDENTIAL_USERNAME),
    'bachelor_imap_summary': (BACHELOR_IMAP_CREDENTIAL_SERVICE, BACHELOR_IMAP_CREDENTIAL_USERNAME),
    'qq_assistant_draft': (ASSISTANT_CREDENTIAL_SERVICE, ASSISTANT_DRAFT_CREDENTIAL_USERNAMES['qq_mail']),
    'bachelor_assistant_draft': (ASSISTANT_CREDENTIAL_SERVICE, ASSISTANT_DRAFT_CREDENTIAL_USERNAMES['bachelor_mail']),
    'bachelor_assistant_smtp': (ASSISTANT_CREDENTIAL_SERVICE, BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME),
    'glm_api_key': (CREDENTIAL_SERVICE, SUMMARY_API_KEY_USERNAME),
    'master_graph_refresh': (CREDENTIAL_SERVICE, MASTER_REFRESH_USERNAME),
}


def _now(now_factory: Callable[[], datetime] | None = None) -> datetime:
    value = (now_factory or (lambda: datetime.now().astimezone()))()
    return value if value.tzinfo is not None and value.utcoffset() is not None else value.astimezone()


def _component(
    component: str,
    status: str,
    summary: str,
    checked_at: str,
    *,
    last_success_at: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        status = 'UNKNOWN'
    return {
        'component': component,
        'status': status,
        'summary': summary,
        'checked_at': checked_at,
        'last_success_at': last_success_at,
        'details': details or {},
    }


def check_mcp_component(checked_at: str, list_tools: Callable[[], Any] | None = None) -> dict[str, Any]:
    try:
        if list_tools is None:
            from windows_gui_mcp import mcp
            tools = asyncio.run(mcp.list_tools())
        else:
            tools = list_tools()
        names = sorted(str(tool.name) for tool in tools)
    except Exception as error:
        return _component('mcp', 'UNKNOWN', 'MCP tools could not be inspected.', checked_at, details={'error_type': type(error).__name__})
    missing = sorted(STABLE_TOOL_NAMES.difference(names))
    unexpected = sorted(set(names).difference(STABLE_TOOL_NAMES))
    exact = not missing and not unexpected and len(names) == len(STABLE_TOOL_NAMES)
    status = 'PASS' if exact else 'FAIL'
    summary = 'All documented MCP interfaces are registered exactly once.' if exact else 'The MCP interface set differs from the documented stable tools.'
    return _component('mcp', status, summary, checked_at, last_success_at=checked_at if exact else None, details={
        'observed_tool_count': len(names),
        'stable_tool_count': len(STABLE_TOOL_NAMES),
        'missing_stable_tools': missing,
        'unexpected_tools': unexpected,
    })


def check_credential_component(
    checked_at: str,
    store_factory: Callable[[str, str], Any] = WindowsCredentialManagerSecretStore,
) -> dict[str, Any]:
    states: dict[str, str] = {}
    for name, (service, username) in CREDENTIAL_CHECKS.items():
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                secret = store_factory(service, username).get_secret()
            states[name] = 'configured' if bool(secret) else 'missing'
            del secret
        except Exception:
            states[name] = 'unknown'
    if any(value == 'unknown' for value in states.values()):
        status, summary = 'UNKNOWN', 'Some credential entries could not be inspected.'
    elif any(value == 'missing' for value in states.values()):
        status, summary = 'WARN', 'Some optional or capability-specific credentials are missing.'
    else:
        status, summary = 'PASS', 'All configured credential entries are present.'
    return _component('mail_credentials', status, summary, checked_at, details={'entries': states, 'verification': 'presence_only'})


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_digest_component(
    checked_at: str,
    *,
    last_run_path: Path | None = None,
    last_attempt_path: Path | None = None,
    now_factory: Callable[[], datetime] | None = None,
    max_age_hours: float = 13.0,
) -> dict[str, Any]:
    run = _read_json(last_run_path or (DIGEST_DIR / 'last-run.json'))
    attempt = _read_json(last_attempt_path or (DIGEST_DIR / 'last-attempt.json'))
    if run is None:
        return _component('mail_digest', 'UNKNOWN', 'No reliable digest run artifact is available.', checked_at, details={'last_attempt': _safe_attempt(attempt)})
    raw_time = str(run.get('generated_at') or '')
    try:
        generated = datetime.fromisoformat(raw_time)
        if generated.tzinfo is None or generated.utcoffset() is None:
            raise ValueError('timezone_missing')
        age_hours = (_now(now_factory).timestamp() - generated.timestamp()) / 3600
    except (TypeError, ValueError, OSError):
        return _component('mail_digest', 'UNKNOWN', 'Digest timestamp is invalid or lacks a timezone.', checked_at, details={'last_attempt': _safe_attempt(attempt)})
    statuses = [str(item.get('status') or 'UNKNOWN') for item in (run.get('mailboxes') or []) if isinstance(item, dict)]
    mailboxes_ok = bool(statuses) and all(value in {'READY', 'EMPTY_TODAY'} for value in statuses)
    fresh = 0 <= age_hours <= max_age_hours
    if mailboxes_ok and fresh:
        status, summary = 'PASS', 'The latest daily mail digest completed successfully.'
    elif mailboxes_ok:
        status, summary = 'WARN', 'The latest successful mail digest is stale.'
    else:
        status, summary = 'FAIL', 'The latest mail digest contains mailbox failures.'
    return _component('mail_digest', status, summary, checked_at, last_success_at=raw_time if mailboxes_ok else None, details={
        'age_hours': round(age_hours, 2),
        'mailbox_statuses': statuses,
        'last_attempt': _safe_attempt(attempt),
    })


def check_scheduled_digest(
    runner: Callable[..., Any] = subprocess.run,
    task_name: str = SCHEDULED_TASK_NAME,
) -> dict[str, Any]:
    """Inspect Task Scheduler without creating, changing, or starting a task."""
    try:
        result = runner(
            ['schtasks', '/query', '/tn', task_name, '/fo', 'LIST', '/v'],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {'status': 'UNKNOWN', 'detail': f'query_failed:{type(error).__name__}'}
    if result.returncode != 0:
        return {'status': 'UNKNOWN', 'detail': 'not_found_or_access_denied'}
    enabled = False
    state_seen = False
    task_status = 'unknown'
    last_result: int | None = None
    last_result_seen = False
    for raw_line in result.stdout.splitlines():
        line = ' '.join(raw_line.casefold().split())
        if line.startswith('scheduled task state:'):
            state_seen = True
            enabled = line == 'scheduled task state: enabled'
        elif line.startswith('status:'):
            task_status = raw_line.split(':', 1)[1].strip()
        elif line.startswith('last result:'):
            last_result_seen = True
            try:
                last_result = int(raw_line.split(':', 1)[1].strip())
            except ValueError:
                last_result = None
    if not state_seen or not last_result_seen:
        status = 'UNKNOWN'
    elif not enabled:
        status = 'FAIL'
    elif last_result is None:
        status = 'UNKNOWN'
    elif last_result != 0:
        status = 'FAIL'
    else:
        status = 'PASS'
    return {
        'status': status, 'enabled': enabled,
        'task_status': task_status, 'last_result': last_result,
    }


def _safe_attempt(attempt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not attempt:
        return None
    return {
        'time': attempt.get('generated_at'),
        'stage': str(attempt.get('stage') or 'UNKNOWN'),
        'ok': attempt.get('ok') is True,
        'error_type': str(attempt.get('error_type') or '') or None,
    }


def check_assistant_component(
    checked_at: str,
    *,
    running: bool | None = None,
    timeout: float = 0.5,
    request_get: Callable[..., Any] = requests.get,
    events: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    detail = 'served_by_current_process'
    if running is None:
        try:
            response = request_get(f'http://127.0.0.1:{ASSISTANT_SERVER_PORT}/api/status', timeout=timeout)
            running = response.status_code == 200
            detail = 'ok' if running else f'http_{response.status_code}'
        except requests.RequestException as error:
            running = False
            detail = f'not_running:{type(error).__name__}'
    relevant = [item for item in (events or []) if item.get('component') == 'mail_assistant']
    successes = [item for item in relevant if item.get('outcome') == 'success']
    last_success = successes[-1].get('time') if successes else None
    return _component(
        'mail_assistant', 'PASS' if running else 'FAIL',
        'The local mail assistant is running.' if running else 'The local mail assistant is not reachable.',
        checked_at, last_success_at=last_success or (checked_at if running else None), details={'service_check': detail},
    )


def check_browser_component(checked_at: str) -> dict[str, Any]:
    endpoints = {
        'qq_mail': os.environ.get('AI_WORK_QQ_CDP_ENDPOINT', '').strip(),
        'bachelor_mail': os.environ.get('AI_WORK_BACHELOR_CDP_ENDPOINT', '').strip(),
    }
    configured = {name: bool(value) for name, value in endpoints.items()}
    invalid = [name for name, value in endpoints.items() if value and not _is_loopback_endpoint(value)]
    if invalid:
        status, summary = 'FAIL', 'A configured CDP endpoint violates the loopback-only policy.'
    elif any(configured.values()):
        status, summary = 'WARN', 'Loopback CDP is configured but not actively probed.'
    else:
        status, summary = 'UNKNOWN', 'Browser session is normally not started and no CDP endpoint is configured.'
    return _component('browser_cdp', status, summary, checked_at, details={
        'browser_session': 'not_observable_cross_process',
        'cdp_configured': configured,
        'invalid_endpoint_mailboxes': invalid,
        'active_probe': False,
    })


def check_remote_component(checked_at: str) -> dict[str, Any]:
    return _component('remote', 'UNKNOWN', 'No reliable, side-effect-free Remote health detector is defined.', checked_at, details={'active_probe': False})


def collect_dashboard_health(
    *,
    now_factory: Callable[[], datetime] | None = None,
    assistant_running: bool | None = None,
    events_path: Path = EVENTS_PATH,
    mcp_list_tools: Callable[[], Any] | None = None,
    credential_store_factory: Callable[[str, str], Any] = WindowsCredentialManagerSecretStore,
    last_run_path: Path | None = None,
    last_attempt_path: Path | None = None,
    request_get: Callable[..., Any] = requests.get,
    task_runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    checked_at = _now(now_factory).isoformat()
    event_report = read_health_events(events_path)
    events = event_report['events']
    digest = check_digest_component(
        checked_at, last_run_path=last_run_path,
        last_attempt_path=last_attempt_path, now_factory=now_factory,
    )
    schedule = check_scheduled_digest(task_runner)
    digest['details']['scheduled_task'] = schedule
    if schedule['status'] == 'FAIL':
        digest['status'] = 'FAIL'
        digest['summary'] = 'The daily mail digest scheduled task is unhealthy.'
    elif schedule['status'] == 'UNKNOWN' and digest['status'] == 'PASS':
        digest['status'] = 'WARN'
        digest['summary'] = 'The latest digest succeeded, but its scheduled task is unknown.'
    components = [
        check_mcp_component(checked_at, mcp_list_tools),
        check_credential_component(checked_at, credential_store_factory),
        digest,
        check_assistant_component(checked_at, running=assistant_running, request_get=request_get, events=events),
        check_browser_component(checked_at),
        check_remote_component(checked_at),
    ]
    recent_errors = [item for item in reversed(events) if item.get('outcome') == 'error'][:10]
    return {
        'checked_at': checked_at,
        'overall_status': _overall_status(components),
        'components': components,
        'recent_errors': recent_errors,
        'event_log': {'invalid_lines': event_report['invalid_lines'], 'displayed_errors': len(recent_errors)},
        'side_effect_free': True,
    }


def _overall_status(components: list[dict[str, Any]]) -> str:
    statuses = {item['status'] for item in components}
    if 'FAIL' in statuses:
        return 'FAIL'
    if 'WARN' in statuses:
        return 'WARN'
    if 'UNKNOWN' in statuses:
        return 'UNKNOWN'
    return 'PASS'


__all__ = ['STATUSES', 'STABLE_TOOL_NAMES', 'collect_dashboard_health']
