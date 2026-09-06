"""Define, inspect, or install the current-user AI-Work startup task."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


TASK_NAME = 'AI-Work Local Assistant'
TASK_PATH = '\\AI-Work\\'
ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'scripts' / 'mail_assistant_server.py'


def startup_definition(python_executable: Path | None = None) -> dict[str, Any]:
    executable = python_executable or Path(sys.executable).with_name('pythonw.exe')
    if not executable.is_file():
        executable = Path(sys.executable)
    return {
        'task_name': TASK_NAME,
        'task_path': TASK_PATH,
        'executable': str(executable.resolve()),
        'arguments': f'"{SERVER.resolve()}" --no-refresh',
        'working_directory': str(ROOT.resolve()),
        'trigger': 'current_user_logon',
        'multiple_instances': 'ignore_new',
        'execution_limit': 'PT0S',
        'start_when_available': True,
    }


class TaskSchedulerAdapter:
    def __init__(self, dispatch=None) -> None:
        if dispatch is None:
            from win32com.client import Dispatch
            dispatch = Dispatch
        self.service = dispatch('Schedule.Service')
        self.service.Connect()

    def _folder(self, create: bool):
        root = self.service.GetFolder('\\')
        try:
            return self.service.GetFolder(TASK_PATH)
        except Exception:
            if not create:
                raise
            return root.CreateFolder('AI-Work')

    def inspect(self) -> dict[str, Any] | None:
        try:
            task = self._folder(False).GetTask(TASK_NAME)
        except Exception:
            return None
        definition = task.Definition
        action = definition.Actions.Item(1)
        return {
            'task_name': TASK_NAME,
            'task_path': TASK_PATH,
            'executable': action.Path,
            'arguments': action.Arguments,
            'working_directory': action.WorkingDirectory,
            'trigger': (
                'current_user_logon'
                if definition.Triggers.Count == 1
                and definition.Triggers.Item(1).Type == 9
                else 'unexpected'
            ),
            'multiple_instances': 'ignore_new' if definition.Settings.MultipleInstances == 2 else 'unexpected',
            'execution_limit': definition.Settings.ExecutionTimeLimit,
            'start_when_available': bool(definition.Settings.StartWhenAvailable),
        }

    def install(self, desired: dict[str, Any]) -> None:
        # Task Scheduler COM constants: logon trigger=9, exec action=0,
        # interactive token=3, create-or-update=6.
        definition = self.service.NewTask(0)
        definition.RegistrationInfo.Description = 'Start the local AI-Work assistant at user logon.'
        trigger = definition.Triggers.Create(9)
        trigger.Enabled = True
        username = os.environ.get('USERNAME', '')
        domain = os.environ.get('USERDOMAIN', '')
        trigger.UserId = f'{domain}\\{username}' if domain else username
        action = definition.Actions.Create(0)
        action.Path = desired['executable']
        action.Arguments = desired['arguments']
        action.WorkingDirectory = desired['working_directory']
        definition.Settings.MultipleInstances = 2
        definition.Settings.ExecutionTimeLimit = desired['execution_limit']
        definition.Settings.StartWhenAvailable = True
        definition.Settings.DisallowStartIfOnBatteries = False
        definition.Settings.StopIfGoingOnBatteries = False
        self._folder(True).RegisterTaskDefinition(
            TASK_NAME, definition, 6, '', '', 3,
        )


def check(adapter, desired: dict[str, Any]) -> dict[str, Any]:
    actual = adapter.inspect()
    if actual is None:
        return {'status': 'MISSING', 'matches': False, 'definition': desired}
    return {
        'status': 'MATCH' if actual == desired else 'DRIFT',
        'matches': actual == desired,
        'definition': desired,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--dry-run', action='store_true')
    group.add_argument('--check', action='store_true')
    group.add_argument('--install', action='store_true')
    args = parser.parse_args(argv)
    desired = startup_definition()
    if args.dry_run:
        print(json.dumps({'status': 'DRY_RUN', 'definition': desired}, ensure_ascii=False, indent=2))
        return 0
    adapter = TaskSchedulerAdapter()
    if args.check:
        result = check(adapter, desired)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['matches'] else 1
    adapter.install(desired)
    result = check(adapter, desired)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['matches'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
