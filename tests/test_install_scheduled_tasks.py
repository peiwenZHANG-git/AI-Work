"""Tests for deterministic recovery of the mail digest scheduled task."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from contextlib import redirect_stdout
import subprocess
import unittest
import json
from unittest import mock


def _load_installer_module():
    path = (
        Path(__file__).resolve().parents[1]
        / 'scripts'
        / 'install_scheduled_tasks.py'
    )
    spec = importlib.util.spec_from_file_location(
        'test_install_scheduled_tasks_target', path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallScheduledTasksTests(unittest.TestCase):
    def setUp(self):
        self.installer = _load_installer_module()

    def test_powershell_quotes_embedded_quotes(self):
        self.assertEqual(
            "'C:\\path with ''quote'''",
            self.installer.powershell_single_quote("C:\\path with 'quote'"),
        )

    def test_prefers_pythonw_when_available(self):
        with mock.patch.object(Path, 'is_file', return_value=True):
            executable = self.installer.digest_python_executable(
                r'C:\Python\python.exe'
            )
        self.assertEqual(Path(r'C:\Python\pythonw.exe'), executable)

    def test_falls_back_to_interpreter_without_pythonw(self):
        with mock.patch.object(Path, 'is_file', return_value=False):
            executable = self.installer.digest_python_executable(
                r'C:\Python\python.exe'
            )
        self.assertEqual(Path(r'C:\Python\python.exe'), executable)

    def test_install_script_registers_both_daily_triggers_safely(self):
        root = Path(r'C:\repo')
        script = self.installer.build_install_script(
            root=root,
            python_executable=r'C:\Python\pythonw.exe',
        )
        self.assertIn("New-ScheduledTaskAction", script)
        self.assertIn("-TaskName 'AI-Work Daily Mail Digest'", script)
        self.assertIn("-Daily -At '10:00'", script)
        self.assertIn("-Daily -At '22:00'", script)
        self.assertIn("-MultipleInstances IgnoreNew", script)
        self.assertIn("-ExecutionTimeLimit (New-TimeSpan -Hours 1)", script)
        self.assertIn("-AllowStartIfOnBatteries:$false", script)
        self.assertIn("-DontStopIfGoingOnBatteries:$false", script)
        self.assertIn("-Force", script)
        self.assertIn('"C:\\repo\\scripts\\daily_mail_digest.py"', script)

    def test_install_task_invokes_powershell_without_side_effect_in_test(self):
        captured = {}

        def runner(command, **kwargs):
            captured['command'] = command
            captured['kwargs'] = kwargs
            return subprocess.CompletedProcess(command, 0, '', '')

        result = self.installer.install_task(
            root=Path(r'C:\repo'),
            python_executable=r'C:\Python\pythonw.exe',
            runner=runner,
        )

        self.assertTrue(result['ok'])
        self.assertEqual('powershell', captured['command'][0])
        self.assertEqual(30, captured['kwargs']['timeout'])
        self.assertIn('-NoProfile', captured['command'])

    def test_nonzero_install_is_explicit_failure(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 2, 'out', 'access denied')

        result = self.installer.install_task(
            root=Path(r'C:\repo'),
            python_executable=r'C:\Python\pythonw.exe',
            runner=runner,
        )
        self.assertFalse(result['ok'])
        self.assertEqual('access denied', result['detail'])

    def test_expected_definition_contains_fixed_safe_settings(self):
        definition = self.installer.expected_task_definition(
            root=Path(r'C:\repo'),
            python_executable=r'C:\Python\pythonw.exe',
        )
        self.assertEqual(r'C:\Python\pythonw.exe', definition['execute'])
        self.assertEqual(
            r'"C:\repo\scripts\daily_mail_digest.py"', definition['arguments']
        )
        self.assertEqual(r'C:\repo', definition['working_directory'])
        self.assertEqual(['10:00', '22:00'], definition['trigger_times'])
        self.assertEqual('IgnoreNew', definition['multiple_instances'])
        self.assertEqual('PT1H', definition['execution_time_limit'])

    def test_check_script_queries_local_task_without_starting_it(self):
        script = self.installer.build_check_script()
        self.assertIn("Get-ScheduledTask -TaskName 'AI-Work Daily Mail Digest'", script)
        self.assertIn('ConvertTo-Json', script)
        self.assertNotIn('Start-ScheduledTask', script)

    def test_check_detects_definition_drift(self):
        desired = {
            'execute': 'pythonw',
            'arguments': '"daily.py"',
            'working_directory': 'repo',
            'trigger_times': ['10:00', '22:00'],
            'multiple_instances': 'IgnoreNew',
            'execution_time_limit': 'PT1H',
        }
        actual = dict(desired, trigger_times='22:00,10:00')
        self.assertEqual([], self.installer.compare_task_definitions(desired, actual))
        actual['execute'] = 'wrong-python'
        actual['working_directory'] = 'elsewhere'
        actual['multiple_instances'] = 'Parallel'
        differences = self.installer.compare_task_definitions(desired, actual)
        self.assertEqual(
            ['execute', 'working_directory', 'multiple_instances'], differences
        )

    def test_check_query_success_reports_definition_state(self):
        desired = self.installer.expected_task_definition(
            root=Path(r'C:\repo'),
            python_executable=r'C:\Python\pythonw.exe',
        )
        actual = {
            'execute': desired['execute'],
            'arguments': desired['arguments'],
            'working_directory': desired['working_directory'],
            'trigger_times': '22:00,10:00',
            'multiple_instances': 'IgnoreNew',
            'execution_time_limit': 'PT1H',
        }

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 0, json.dumps(actual), ''
            )

        result = self.installer.check_task_definition(
            root=Path(r'C:\repo'),
            python_executable=r'C:\Python\pythonw.exe',
            runner=runner,
        )
        self.assertTrue(result['ok'])
        self.assertEqual([], result['differences'])

    def test_check_query_failure_is_explicit(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 2, '', 'access denied')

        result = self.installer.check_task_definition(
            root=Path(r'C:\repo'),
            python_executable=r'C:\Python\pythonw.exe',
            runner=runner,
        )
        self.assertFalse(result['ok'])
        self.assertEqual(['query_failed'], result['differences'])
        self.assertEqual('access denied', result['detail'])

    def test_dry_run_prints_command_without_registering(self):
        output = []
        with mock.patch.object(
            self.installer, 'install_task'
        ) as install_task, mock.patch.object(
            self.installer, 'build_install_script', return_value='SCRIPT'
        ):
            with redirect_stdout(io.StringIO()):
                exit_code = self.installer.main(
                    [
                        '--root', r'C:\repo',
                        '--python-executable', r'C:\Python\pythonw.exe',
                        '--dry-run',
                    ],
                )

        self.assertEqual(0, exit_code)
        install_task.assert_not_called()

    def test_check_mode_prints_json_and_does_not_install(self):
        result_payload = {
            'ok': True,
            'differences': [],
            'detail': 'definition_matches',
            'desired': {},
            'actual': {},
        }
        output = io.StringIO()
        with mock.patch.object(
            self.installer,
            'check_task_definition',
            return_value=result_payload,
        ) as check, mock.patch.object(
            self.installer, 'install_task'
        ) as install_task, redirect_stdout(
            output
        ):
            exit_code = self.installer.main(
                [
                    '--root', r'C:\repo',
                    '--python-executable', r'C:\Python\pythonw.exe',
                    '--check',
                ],
            )

        self.assertEqual(0, exit_code)
        check.assert_called_once()
        install_task.assert_not_called()
        self.assertIn('definition_matches', output.getvalue())


if __name__ == '__main__':
    unittest.main()
