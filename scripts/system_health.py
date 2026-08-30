"""Read-only local health checks for the AI-Work automation baseline."""

from __future__ import annotations

import argparse
import asyncio
import io
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from windows_gui.mail_assistant import (
    ASSISTANT_CREDENTIAL_SERVICE,
    ASSISTANT_DRAFT_CREDENTIAL_USERNAMES,
    BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME,
)
from windows_gui.mail_backends import WindowsCredentialManagerSecretStore
from windows_gui.mail_digest import (
    CREDENTIAL_SERVICE,
    DIGEST_DIR,
    MASTER_REFRESH_USERNAME,
    SUMMARY_API_KEY_USERNAME,
    ensure_environment,
)
from windows_gui.imap_mail import (
    BACHELOR_IMAP_CREDENTIAL_SERVICE,
    BACHELOR_IMAP_CREDENTIAL_USERNAME,
    QQ_IMAP_CREDENTIAL_SERVICE,
    QQ_IMAP_CREDENTIAL_USERNAME,
)
from windows_gui_mcp import mcp


ASSISTANT_SERVER_PORT = 8931
SCHEDULED_TASK_NAME = 'AI-Work Daily Mail Digest'
EXPECTED_TOOL_COUNT = 28
ENVIRONMENT_VARIABLES = (
    'AI_WORK_QQ_IMAP_USERNAME',
    'AI_WORK_BACHELOR_IMAP_USERNAME',
    'AI_WORK_OUTLOOK_TENANT_ID',
    'AI_WORK_OUTLOOK_CLIENT_ID',
    'AI_WORK_OUTLOOK_MAILBOX',
)
CREDENTIAL_CHECKS = {
    'qq_imap_summary': (QQ_IMAP_CREDENTIAL_SERVICE, QQ_IMAP_CREDENTIAL_USERNAME),
    'bachelor_imap_summary': (
        BACHELOR_IMAP_CREDENTIAL_SERVICE,
        BACHELOR_IMAP_CREDENTIAL_USERNAME,
    ),
    'qq_assistant_draft': (
        ASSISTANT_CREDENTIAL_SERVICE,
        ASSISTANT_DRAFT_CREDENTIAL_USERNAMES['qq_mail'],
    ),
    'bachelor_assistant_draft': (
        ASSISTANT_CREDENTIAL_SERVICE,
        ASSISTANT_DRAFT_CREDENTIAL_USERNAMES['bachelor_mail'],
    ),
    'bachelor_assistant_smtp': (
        ASSISTANT_CREDENTIAL_SERVICE,
        BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME,
    ),
    'glm_api_key': (CREDENTIAL_SERVICE, SUMMARY_API_KEY_USERNAME),
    'master_graph_refresh': (CREDENTIAL_SERVICE, MASTER_REFRESH_USERNAME),
}


def check_environment(names: tuple[str, ...] = ENVIRONMENT_VARIABLES) -> dict[str, Any]:
    configured = {name: bool(os.environ.get(name, '').strip()) for name in names}
    return {
        'ok': all(configured.values()),
        'configured': configured,
    }


def check_credentials(
    checks: dict[str, tuple[str, str]] = CREDENTIAL_CHECKS,
    store_factory: Callable[[str, str], Any] = WindowsCredentialManagerSecretStore,
) -> dict[str, Any]:
    configured: dict[str, bool] = {}
    for name, (service, username) in checks.items():
        try:
            # Some keyring backends emit advisory text on stdout; suppress it
            # so JSON diagnostics remain valid. The secret value is discarded.
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                secret = store_factory(service, username).get_secret()
                configured[name] = bool(secret)
                del secret
        except Exception:
            configured[name] = False
    return {'ok': all(configured.values()), 'configured': configured}


def check_mcp_tools(expected_count: int = EXPECTED_TOOL_COUNT) -> dict[str, Any]:
    tools = asyncio.run(mcp.list_tools())
    names = sorted(tool.name for tool in tools)
    return {
        'ok': len(names) == expected_count,
        'count': len(names),
        'expected_count': expected_count,
        'names': names,
    }


def parse_scheduled_task(output: str) -> dict[str, Any]:
    enabled = False
    status = 'unknown'
    last_result: int | None = None
    for raw_line in output.splitlines():
        line = ' '.join(raw_line.casefold().split())
        if line.startswith('scheduled task state:'):
            enabled = line == 'scheduled task state: enabled'
        elif line.startswith('status:'):
            status = raw_line.split(':', 1)[1].strip()
        elif line.startswith('last result:'):
            try:
                last_result = int(raw_line.split(':', 1)[1].strip())
            except ValueError:
                last_result = None
    return {
        'enabled': enabled,
        'status': status,
        'last_result': last_result,
    }


def check_scheduled_task(
    task_name: str = SCHEDULED_TASK_NAME,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    try:
        result = runner(
            ['schtasks', '/query', '/tn', task_name, '/fo', 'LIST', '/v'],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {
            'ok': False,
            'enabled': False,
            'status': f'query_failed:{type(error).__name__}',
            'last_result': None,
        }
    if result.returncode != 0:
        return {
            'ok': False,
            'enabled': False,
            'status': 'not_found_or_access_denied',
            'last_result': None,
        }
    parsed = parse_scheduled_task(result.stdout)
    parsed['ok'] = parsed['enabled'] and parsed['last_result'] in (None, 0)
    return parsed


def check_assistant_server(timeout: float = 0.5) -> dict[str, Any]:
    try:
        response = requests.get(
            f'http://127.0.0.1:{ASSISTANT_SERVER_PORT}/api/status',
            timeout=timeout,
        )
        running = response.status_code == 200
        detail = 'ok' if running else f'http_{response.status_code}'
    except requests.RequestException as error:
        running = False
        detail = f'not_running:{type(error).__name__}'
    return {
        'ok': running,
        'running': running,
        'detail': detail,
    }


def summarize_last_run(data: dict[str, Any]) -> dict[str, Any]:
    mailboxes = []
    for item in data.get('mailboxes') or []:
        mailbox_id = str(item.get('mailbox_id') or 'unknown')
        mailboxes.append({
            'id': mailbox_id,
            'status': str(item.get('status') or 'UNKNOWN'),
            'count': int(item.get('count') or 0),
        })
    healthy_statuses = {'READY', 'EMPTY_TODAY'}
    all_mailboxes_ok = bool(mailboxes) and all(
        mailbox['status'] in healthy_statuses for mailbox in mailboxes
    )
    return {
        'generated_at': data.get('generated_at'),
        'run_ok': data.get('ok') is True,
        'all_mailboxes_ok': all_mailboxes_ok,
        'mailboxes': mailboxes,
    }


def check_last_digest(stats_path: Path | None = None) -> dict[str, Any]:
    path = stats_path or (DIGEST_DIR / 'last-run.json')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        return {
            'ok': False,
            'detail': f'not_available:{type(error).__name__}',
            'last_run': None,
        }
    summary = summarize_last_run(data)
    ok = bool(summary['run_ok'] and summary['all_mailboxes_ok'])
    return {
        'ok': ok,
        'detail': 'ok' if ok else 'last_run_reported_failure',
        'last_run': summary,
    }


def collect_health(
    *,
    assistant_timeout: float = 0.5,
    task_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    checks = [
        {
            'name': 'environment_configuration',
            'required': True,
            **check_environment(),
        },
        {
            'name': 'credential_manager_entries',
            'required': True,
            **check_credentials(),
        },
        {
            'name': 'fastmcp_tools',
            'required': True,
            **check_mcp_tools(),
        },
        {
            'name': 'scheduled_mail_digest',
            'required': True,
            **check_scheduled_task(runner=task_runner),
        },
        {
            'name': 'last_mail_digest',
            'required': True,
            **check_last_digest(),
        },
        {
            'name': 'assistant_server',
            'required': False,
            **check_assistant_server(timeout=assistant_timeout),
        },
    ]
    return {
        'ok': all(check['ok'] for check in checks if check['required']),
        'checks': checks,
    }


def render_report(report: dict[str, Any]) -> str:
    lines = ['AI-Work system health']
    for check in report['checks']:
        marker = 'PASS' if check['ok'] else ('INFO' if not check['required'] else 'FAIL')
        lines.append(f"[{marker}] {check['name']}")
    lines.append('Overall: PASS' if report['ok'] else 'Overall: FAIL')
    lines.append(
        'MANUAL CHECK: real desktop GUI and live mailbox authorization require explicit user authorization.'
    )
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='print the JSON report')
    args = parser.parse_args(argv)
    ensure_environment()
    report = collect_health()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_report(report))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
