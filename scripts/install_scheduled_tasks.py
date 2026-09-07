"""Install/check one fixed Windows task: mail digest, computer brief, or important mail check."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable


TASK_NAME = 'AI-Work Daily Mail Digest'
DIGEST_TIMES = ('10:00', '22:00')
BRIEF_TASK_NAME = 'AI-Work Daily Computer Brief'
IMPORTANT_TASK_NAME = 'AI-Work Important Mail Check'
IMPORTANT_TRIGGER_START = '2026-08-29T00:00:00'
IMPORTANT_TRIGGER_INTERVAL = 'PT1H'
IMPORTANT_TRIGGER_DURATION = 'P3650D'


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
    computer_brief: bool = False,
    important_mail_check: bool = False,
) -> str:
    if computer_brief and important_mail_check:
        raise ValueError('select only one scheduled task')
    if computer_brief:
        action_script = root / 'scripts' / 'daily_computer_brief.py'
        task_name = BRIEF_TASK_NAME
        timeout = '-Minutes 15'
        battery = ''
        catchup = ' -StartWhenAvailable'
        principal = (
            '$principal = New-ScheduledTaskPrincipal '
            '-UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) '
            '-LogonType Interactive -RunLevel Limited\n'
        )
        principal_argument = '-Principal $principal '
        action_arguments = f'"{action_script}"'
        triggers = ", ".join(
            f"(New-ScheduledTaskTrigger -Daily -At {powershell_single_quote(time)})"
            for time in ('08:00',)
        )
    elif important_mail_check:
        action_script = root / 'scripts' / 'daily_mail_digest.py'
        task_name = IMPORTANT_TASK_NAME
        timeout = '-Minutes 10'
        battery = ':$false'
        catchup = ' -StartWhenAvailable'
        principal = (
            '$principal = New-ScheduledTaskPrincipal '
            '-UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) '
            '-LogonType Interactive -RunLevel Limited\n'
        )
        principal_argument = '-Principal $principal '
        action_arguments = f'"{action_script}" --check-high'
        triggers = '$trigger'
    else:
        action_script = root / 'scripts' / 'daily_mail_digest.py'
        task_name = TASK_NAME
        timeout = '-Hours 1'
        battery = ':$false'
        catchup = ''
        principal = ''
        principal_argument = ''
        action_arguments = f'"{action_script}"'
        triggers = ", ".join(
            f"(New-ScheduledTaskTrigger -Daily -At {powershell_single_quote(time)})"
            for time in DIGEST_TIMES
        )
    important_trigger = ''
    if important_mail_check:
        important_trigger = f"""
$trigger = New-ScheduledTaskTrigger -Once -At {powershell_single_quote(IMPORTANT_TRIGGER_START)}
$trigger.Repetition.Interval = {powershell_single_quote(IMPORTANT_TRIGGER_INTERVAL)}
$trigger.Repetition.Duration = {powershell_single_quote(IMPORTANT_TRIGGER_DURATION)}
$trigger.Repetition.StopAtDurationEnd = $true
"""
    return f"""
{important_trigger}$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction `
    -Execute {powershell_single_quote(python_executable)} `
    -Argument {powershell_single_quote(action_arguments)} `
    -WorkingDirectory {powershell_single_quote(str(root))}
$triggers = {triggers}
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan {timeout}) `
    -AllowStartIfOnBatteries{battery} `
    -DontStopIfGoingOnBatteries{battery}{catchup}
{principal}Register-ScheduledTask {principal_argument}`
    -TaskName {powershell_single_quote(task_name)} `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Force | Out-Null
"""


def expected_task_definition(
    *,
    root: Path,
    python_executable: str,
    computer_brief: bool = False,
    important_mail_check: bool = False,
) -> dict[str, Any]:
    if computer_brief and important_mail_check:
        raise ValueError('select only one scheduled task')
    if important_mail_check:
        executable = digest_python_executable(python_executable)
        action_script = root / 'scripts' / 'daily_mail_digest.py'
        return {
            'execute': str(executable),
            'arguments': f'"{action_script}" --check-high',
            'working_directory': str(root),
            'trigger_times': ['00:00'],
            'multiple_instances': 'IgnoreNew',
            'execution_time_limit': 'PT10M',
            'disallow_start_on_batteries': True,
            'stop_if_going_on_batteries': True,
            'start_when_available': True,
            'action_count': 1,
            'trigger_count': 1,
            'trigger_type': 'MSFT_TaskTimeTrigger',
            'daily_trigger_count': 0,
            'daily_interval': None,
            'repetition_interval': IMPORTANT_TRIGGER_INTERVAL,
            'repetition_duration': IMPORTANT_TRIGGER_DURATION,
            'repetition_stop_at_duration_end': True,
            'repetition_disabled': False,
            'enabled': True,
            'principal_current_user': True,
            'logon_type': 'Interactive',
            'run_level': 'Limited',
        }
    if computer_brief:
        desired = expected_task_definition(root=root, python_executable=python_executable)
        desired.update(arguments=f'"{root / "scripts" / "daily_computer_brief.py"}"',
                       trigger_times=['08:00'], execution_time_limit='PT15M',
                       disallow_start_on_batteries=False, stop_if_going_on_batteries=False,
                       action_count=1, daily_trigger_count=1, trigger_count=1,
                       daily_interval=1, repetition_disabled=True, enabled=True,
                       principal_current_user=True, logon_type='Interactive', run_level='Limited',
                       start_when_available=True)
        return desired
    executable = digest_python_executable(python_executable)
    action_script = root / 'scripts' / 'daily_mail_digest.py'
    return {
        'execute': str(executable),
        'arguments': f'"{action_script}"',
        'working_directory': str(root),
        'trigger_times': sorted(DIGEST_TIMES),
        'multiple_instances': 'IgnoreNew',
        'execution_time_limit': 'PT1H',
        'disallow_start_on_batteries': True,
        'stop_if_going_on_batteries': True,
    }


def build_check_script(task_name: str = TASK_NAME) -> str:
    task_name_literal = powershell_single_quote(task_name)
    return f"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName {task_name_literal}
$action = @($task.Actions)[0]
$principalMatches = $false
try {{
    $principalId = [string]$task.Principal.UserId
    $principalSid = if ($principalId.StartsWith('S-1-')) {{
        [System.Security.Principal.SecurityIdentifier]::new($principalId)
    }} else {{
        ([System.Security.Principal.NTAccount]::new($principalId)).Translate([System.Security.Principal.SecurityIdentifier])
    }}
    $principalMatches = $principalSid.Value -eq [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
}} catch {{ $principalMatches = $false }}
$triggerTimes = @(
    $task.Triggers | ForEach-Object {{ ([datetime]$_.StartBoundary).ToString('HH:mm') }}
) -join ','
$triggerType = @(
    $task.Triggers | ForEach-Object {{ $_.CimClass.CimClassName }}
) -join ','
$firstTrigger = @($task.Triggers)[0]
[pscustomobject]@{{
    execute = [string]$action.Execute
    arguments = [string]$action.Arguments
    working_directory = [string]$action.WorkingDirectory
    trigger_times = [string]$triggerTimes
    multiple_instances = [string]$task.Settings.MultipleInstances
    execution_time_limit = [string]$task.Settings.ExecutionTimeLimit
    disallow_start_on_batteries = [bool]$task.Settings.DisallowStartIfOnBatteries
    stop_if_going_on_batteries = [bool]$task.Settings.StopIfGoingOnBatteries
    start_when_available = [bool]$task.Settings.StartWhenAvailable
    action_count = @($task.Actions).Count
    trigger_count = @($task.Triggers).Count
    trigger_type = [string]$triggerType
    daily_trigger_count = @($task.Triggers | Where-Object {{ $_.CimClass.CimClassName -eq 'MSFT_TaskDailyTrigger' }}).Count
    daily_interval = @($task.Triggers)[0].DaysInterval
    repetition_interval = [string]$firstTrigger.Repetition.Interval
    repetition_duration = [string]$firstTrigger.Repetition.Duration
    repetition_stop_at_duration_end = [bool]$firstTrigger.Repetition.StopAtDurationEnd
    repetition_disabled = -not [bool](@($task.Triggers)[0].Repetition.Interval)
    enabled = [bool]$task.Settings.Enabled -and [bool](@($task.Triggers)[0].Enabled)
    principal_current_user = $principalMatches
    logon_type = [string]$task.Principal.LogonType
    run_level = [string]$task.Principal.RunLevel
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
        'disallow_start_on_batteries': actual.get('disallow_start_on_batteries') is True,
        'stop_if_going_on_batteries': actual.get('stop_if_going_on_batteries') is True,
        'trigger_type': str(actual.get('trigger_type') or ''),
        'repetition_interval': str(actual.get('repetition_interval') or ''),
        'repetition_duration': str(actual.get('repetition_duration') or ''),
        'repetition_stop_at_duration_end': actual.get('repetition_stop_at_duration_end') is True,
    }
    for key in ('action_count', 'trigger_count', 'daily_trigger_count', 'daily_interval',
                'repetition_disabled', 'enabled', 'principal_current_user', 'logon_type', 'run_level', 'start_when_available'):
        normalized_actual[key] = actual.get(key)
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
    computer_brief: bool = False,
    important_mail_check: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    desired = expected_task_definition(
        root=root,
        python_executable=python_executable,
        computer_brief=computer_brief,
        important_mail_check=important_mail_check,
    )
    if computer_brief:
        task_name = BRIEF_TASK_NAME
    elif important_mail_check:
        task_name = IMPORTANT_TASK_NAME
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
    computer_brief: bool = False,
    important_mail_check: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    executable = digest_python_executable(python_executable)
    command = [
        'powershell',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-Command',
        build_install_script(
            root=root,
            python_executable=str(executable),
            computer_brief=computer_brief,
            important_mail_check=important_mail_check,
        ),
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
    parser.add_argument('--computer-brief', action='store_true',
                        help='select only the independent 08:00 computer brief task')
    parser.add_argument(
        '--important-mail-check', action='store_true',
        help='select only the independent hourly important mail check task',
    )
    args = parser.parse_args(argv)
    if args.computer_brief and args.important_mail_check:
        parser.error('--computer-brief and --important-mail-check are mutually exclusive')
    task_options = {}
    if args.computer_brief:
        task_options['computer_brief'] = True
    if args.important_mail_check:
        task_options['important_mail_check'] = True
    if args.dry_run and args.check:
        parser.error('--dry-run and --check are mutually exclusive')
    root = args.root.resolve()
    if args.check:
        result = check_task_definition(
            root=root,
            python_executable=args.python_executable,
            **task_options,
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
        build_install_script(root=root, python_executable=str(executable), **task_options),
    ]
    if args.dry_run:
        print(command[-1])
        return 0
    result = install_task(
        root=root,
        python_executable=str(executable),
        **task_options,
    )
    selected_task_name = (
        BRIEF_TASK_NAME
        if args.computer_brief
        else IMPORTANT_TASK_NAME
        if args.important_mail_check
        else TASK_NAME
    )
    print(f"[{'PASS' if result['ok'] else 'FAIL'}] {selected_task_name}: {result['detail']}")
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
