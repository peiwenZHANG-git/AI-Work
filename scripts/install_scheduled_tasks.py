"""Idempotently install the local mail digest Windows scheduled task."""

from __future__ import annotations

import argparse
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
    triggers = ', '.join(
        f"(New-ScheduledTaskTrigger -Daily -At {powershell_single_quote(time)})"
        for time in DIGEST_TIMES
    )
    return f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction `
    -Execute {powershell_single_quote(python_executable)} `
    -Argument {powershell_single_quote(str(action_script))} `
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
    args = parser.parse_args(argv)
    root = args.root.resolve()
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
