"""Idempotently install the local mail digest Windows scheduled task."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable


TASK_NAME = 'AI-Work Daily Mail Digest'
DIGEST_TIMES = ('10:00', '22:00')


def powershell_single_quote(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def digest_python_executable(python_executable: str | None = None) -> Path:
    current = Path(python_executable or sys.executable)
    pythonw = current.with_name('pythonw.exe')
    if current.name.casefold() == 'pythonw.exe':
        return current
    return pythonw if pythonw.is_file() else current


def build_install_script(
    *,
    root: Path,
    python_executable: str,
) -> str:
    action_script = root / 'scripts' / 'daily_mail_digest.py'
    action_arguments = f'"{action_script}"'
    triggers = ', '.join(
        f"(New-ScheduledTaskTrigger -Daily -At {powershell_single_quote(time)})"
        for time in DIGEST_TIMES
    )
    return f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction `
    -Execute {powershell_single_quote(python_executable)} `
    -Argument {powershell_single_quote(action_arguments)} `
    -WorkingDirectory {powershell_single_quote(str(root))}
$triggers = {triggers}
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -DisallowStartIfOnBatteries `
    -StopIfGoingOnBatteries
Register-ScheduledTask `
    -TaskName {powershell_single_quote(TASK_NAME)} `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Force | Out-Null
"""


def expected_task_definition(
    *,
    root: Path,
    python_executable: str,
) -> dict[str, Any]:
    executable = digest_python_executable(python_executable)
    action_script = root / 'scripts' / 'daily_mail_digest.py'
    return {
        'execute': str(executable),
        'arguments': f'"{action_script}"',
        'working_directory': str(root),
        'trigger_times': sorted(DIGEST_TIMES),
        'multiple_instances': 'IgnoreNew',
        'execution_time_limit': '01:00:00',
    }


def build_check_script(task_name: str = TASK_NAME) -> str:
    task_name_literal = powershell_single_quote(task_name)
    return f"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName {task_name_literal}
$action = @($task.Actions)[0]
$triggerTimes = @(
    $task.Triggers | ForEach-Object {{ ([datetime]$_.StartBoundary).ToString('HH:mm') }}
) -join ','
[pscustomobject]@{{
    execute = [string]$action.Execute
    arguments = [string]$action.Arguments
    working_directory = [string]$action.WorkingDirectory
    trigger_times = [string]$triggerTimes
    multiple_instances = [string]$task.Settings.MultipleInstances
    execution_time_limit = [string]$task.Settings.ExecutionTimeLimit
}} | ConvertTo-Json -Compress
"""


def compare_task_definitions(
    desired: dict[str, Any], actual: dict[str, Any]
) -> list[str]:
    actual_trigger_times = sorted(
        item.strip()
        for item in str(actual.get('trigger_times') or '').split(',')
        if item.strip()
    )
    normalized_actual = {
        'execute': str(actual.get('execute') or ''),
        'arguments': str(actual.get('arguments') or ''),
        'working_directory': str(actual.get('working_directory') or ''),
        'trigger_times': actual_trigger_times,
        'multiple_instances': str(actual.get('multiple_instances') or ''),
        'execution_time_limit': str(actual.get('execution_time_limit') or ''),
    }
    differences = []
    for key, expected_value in desired.items():
        actual_value = normalized_actual[key]
        if key in ('execute', 'arguments', 'working_directory'):
            matches = actual_value.casefold() == str(expected_value).casefold()
        else:
            matches = actual_value == expected_value
        if not matches:
            differences.append(key)
    return differences


def check_task_definition(
    *,
    root: Path,
    python_executable: str,
    task_name: str = TASK_NAME,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    desired = expected_task_definition(
        root=root, python_executable=python_executable
    )
    command = [
        'powershell',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-Command',
        build_check_script(task_name),
    ]
    try:
        result = runner(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        return {
            'ok': False,
            'differences': ['query_failed'],
            'detail': f'query_failed:{type(error).__name__}',
            'desired': desired,
            'actual': {},
        }
    if result.returncode != 0:
        return {
            'ok': False,
            'differences': ['query_failed'],
            'detail': result.stderr.strip() or result.stdout.strip() or (
                f'powershell exit {result.returncode}'
            ),
            'desired': desired,
            'actual': {},
        }
    try:
        actual = json.loads(result.stdout)
        if not isinstance(actual, dict):
            raise ValueError('task definition is not an object')
    except ValueError as error:
        return {
            'ok': False,
            'differences': ['invalid_query_output'],
            'detail': f'invalid_query_output:{type(error).__name__}',
            'desired': desired,
            'actual': {},
        }
    differences = compare_task_definitions(desired, actual)
    return {
        'ok': not differences,
        'differences': differences,
        'detail': 'definition_matches' if not differences else 'definition_drift',
        'desired': desired,
        'actual': actual,
    }


def install_task(
    *,
    root: Path,
    python_executable: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    executable = digest_python_executable(python_executable)
    command = [
        'powershell',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-Command',
        build_install_script(root=root, python_executable=str(executable)),
    ]
    try:
        result = runner(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return {
            'ok': False,
            'detail': f'installation_failed:{type(error).__name__}',
            'python_executable': str(executable),
        }
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or (
            f'powershell exit {result.returncode}'
        )
        return {
            'ok': False,
            'detail': detail,
            'python_executable': str(executable),
        }
    return {
        'ok': True,
        'detail': 'installed',
        'python_executable': str(executable),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--root',
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help='repository root (default: script parent directory)',
    )
    parser.add_argument(
        '--python-executable',
        default=sys.executable,
        help='interpreter to schedule; defaults to the current interpreter',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='print the generated PowerShell command without registering',
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='read and compare the registered task definition without changing it',
    )
    args = parser.parse_args(argv)
    if args.dry_run and args.check:
        parser.error('--dry-run and --check are mutually exclusive')
    root = args.root.resolve()
    if args.check:
        result = check_task_definition(
            root=root,
            python_executable=args.python_executable,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['ok'] else 1
    executable = digest_python_executable(args.python_executable)
    command = [
        'powershell',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-Command',
        build_install_script(root=root, python_executable=str(executable)),
    ]
    if args.dry_run:
        print(command[-1])
        return 0
    result = install_task(
        root=root,
        python_executable=str(executable),
    )
    print(f"[{'PASS' if result['ok'] else 'FAIL'}] {TASK_NAME}: {result['detail']}")
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
