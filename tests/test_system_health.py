"""Tests for the side-effect-free system health report."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock


def _load_health_module():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'system_health.py'
    spec = importlib.util.spec_from_file_location('test_system_health_target', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SystemHealthTests(unittest.TestCase):
    def setUp(self):
        self.health = _load_health_module()

    def test_environment_reports_presence_without_values(self):
        with mock.patch.dict(
            'os.environ',
            {
                'AI_WORK_QQ_IMAP_USERNAME': 'secret-user',
                'AI_WORK_BACHELOR_IMAP_USERNAME': '',
                'AI_WORK_OUTLOOK_TENANT_ID': 'tenant',
                'AI_WORK_OUTLOOK_CLIENT_ID': 'client',
                'AI_WORK_OUTLOOK_MAILBOX': 'mailbox',
            },
        ):
            result = self.health.check_environment()

        self.assertFalse(result['ok'])
        self.assertEqual(
            {
                'AI_WORK_QQ_IMAP_USERNAME': True,
                'AI_WORK_BACHELOR_IMAP_USERNAME': False,
                'AI_WORK_OUTLOOK_TENANT_ID': True,
                'AI_WORK_OUTLOOK_CLIENT_ID': True,
                'AI_WORK_OUTLOOK_MAILBOX': True,
            },
            result['configured'],
        )
        self.assertNotIn('secret-user', repr(result))

    def test_credential_check_reports_presence_and_survives_errors(self):
        def store_factory(service, username):
            if username == 'broken':
                raise OSError('access denied')
            return SimpleNamespace(get_secret=lambda: 'runtime-secret')

        result = self.health.check_credentials(
            {
                'present': ('service', 'present'),
                'broken': ('service', 'broken'),
            },
            store_factory=store_factory,
        )

        self.assertFalse(result['ok'])
        self.assertEqual({'present': True, 'broken': False}, result['configured'])
        self.assertNotIn('runtime-secret', repr(result))

    def test_credential_advisory_output_is_suppressed(self):
        import io
        from contextlib import redirect_stdout

        def store_factory(service, username):
            def get_secret():
                print('Retrieved a credential advisory')
                return 'runtime-secret'
            return SimpleNamespace(get_secret=get_secret)

        output = io.StringIO()
        with redirect_stdout(output):
            result = self.health.check_credentials(
                {'entry': ('service', 'username')},
                store_factory=store_factory,
            )

        self.assertEqual('', output.getvalue())
        self.assertTrue(result['ok'])

    def test_parses_scheduled_task_without_returning_raw_output(self):
        parsed = self.health.parse_scheduled_task(
            'HostName: private-host\n'
            'Status: Ready\n'
            'Last Result: 0\n'
            'Run As User: private-user\n'
            'Scheduled Task State: Enabled\n'
        )
        self.assertTrue(parsed['enabled'])
        self.assertTrue(parsed['enabled'])
        self.assertEqual('Ready', parsed['status'])
        self.assertEqual(0, parsed['last_result'])

    def test_query_failure_is_a_failure(self):
        def runner(*args, **kwargs):
            raise OSError('schtasks unavailable')

        result = self.health.check_scheduled_task(runner=runner)
        self.assertFalse(result['ok'])
        self.assertEqual('query_failed:OSError', result['status'])

    def test_scheduled_task_query_requests_verbose_fields(self):
        captured = {}

        def runner(command, **kwargs):
            captured['command'] = command
            return subprocess.CompletedProcess(command, 0, '', '')

        result = self.health.check_scheduled_task(runner=runner)

        self.assertIn('/v', captured['command'])
        self.assertFalse(result['ok'])

    def test_task_definition_check_parses_installer_report(self):
        payload = {
            'ok': False,
            'detail': 'definition_drift',
            'differences': ['execution_time_limit'],
        }

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 1, json.dumps(payload), ''
            )

        result = self.health.check_task_definition(runner=runner)
        self.assertFalse(result['ok'])
        self.assertEqual('definition_drift', result['detail'])
        self.assertEqual(['execution_time_limit'], result['differences'])

    def test_task_definition_check_rejects_invalid_output(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, 'not-json', '')

        result = self.health.check_task_definition(runner=runner)
        self.assertFalse(result['ok'])
        self.assertEqual('invalid_check_output', result['detail'])

    def test_last_run_summary_contains_status_not_message_content(self):
        now = datetime(2026, 8, 31, 11, 0, tzinfo=timezone(timedelta(hours=2)))
        summary = self.health.summarize_last_run({
            'generated_at': '2026-08-31T10:00:00+02:00',
            'toast_shown': True,
            'message': 'private mail sentence',
            'mailboxes': [
                {'mailbox_id': 'qq_mail', 'status': 'EMPTY_TODAY', 'count': 0},
                {'mailbox_id': 'master_mail', 'status': 'READY', 'count': 2},
            ],
        }, now_factory=lambda: now)
        self.assertTrue(summary['fresh'])
        self.assertEqual(1, summary['age_hours'])
        self.assertTrue(summary['toast_shown'])
        self.assertTrue(summary['all_mailboxes_ok'])
        self.assertNotIn('private mail sentence', repr(summary))

    def test_last_attempt_summary_contains_status_not_message_content(self):
        summary = self.health.summarize_last_attempt({
            'generated_at': '2026-09-01T10:00:00+02:00',
            'stage': 'exception',
            'ok': False,
            'skipped': False,
            'error_type': 'RuntimeError',
            'detail': 'private failure sentence',
            'mailboxes': [
                {'mailbox_id': 'qq_mail', 'status': 'READY', 'count': 1},
            ],
        })
        self.assertEqual('exception', summary['stage'])
        self.assertFalse(summary['ok'])
        self.assertEqual('RuntimeError', summary['error_type'])
        self.assertNotIn('private failure sentence', repr(summary))

    def test_stale_last_run_reports_attempt_stage(self):
        now = datetime(2026, 9, 1, 10, 0, tzinfo=timezone(timedelta(hours=2)))
        with tempfile.TemporaryDirectory() as directory:
            run_path = Path(directory) / 'last-run.json'
            attempt_path = Path(directory) / 'last-attempt.json'
            run_path.write_text(json.dumps({
                'generated_at': '2026-09-01T10:00:00+02:00',
                'toast_shown': True,
                'mailboxes': [],
            }), encoding='utf-8')
            attempt_path.write_text(json.dumps({
                'stage': 'artifact_write',
                'ok': False,
                'error_type': 'PermissionError',
                'detail': 'private failure sentence',
            }), encoding='utf-8')
            result = self.health.check_last_digest(
                run_path,
                now_factory=lambda: now,
                attempt_path=attempt_path,
            )

        self.assertFalse(result['ok'])
        self.assertIn('attempt_stage:artifact_write', result['detail'])
        self.assertIn('attempt_error:PermissionError', result['detail'])

    def test_mail_digest_is_healthy_even_if_toast_was_not_shown(self):
        now = datetime(2026, 8, 31, 11, 0, tzinfo=timezone(timedelta(hours=2)))
        data = {
            'generated_at': '2026-08-31T10:00:00+02:00',
            'toast_shown': False,
            'mailboxes': [
                {'mailbox_id': 'qq_mail', 'status': 'EMPTY_TODAY', 'count': 0},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'last-run.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            result = self.health.check_last_digest(path, now_factory=lambda: now)

        self.assertTrue(result['ok'])
        self.assertEqual('ok', result['detail'])
        self.assertFalse(result['last_run']['toast_shown'])

    def test_stale_mail_digest_is_required_failure(self):
        now = datetime(2026, 8, 31, 11, 0, tzinfo=timezone(timedelta(hours=2)))
        data = {
            'generated_at': '2026-08-30T10:00:00+02:00',
            'toast_shown': True,
            'mailboxes': [
                {'mailbox_id': 'qq_mail', 'status': 'EMPTY_TODAY', 'count': 0},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'last-run.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            result = self.health.check_last_digest(path, now_factory=lambda: now)

        self.assertFalse(result['ok'])
        self.assertEqual('stale:25.0h', result['detail'])

    def test_invalid_generated_at_is_stale(self):
        now = datetime(2026, 8, 31, 11, 0, tzinfo=timezone(timedelta(hours=2)))
        data = {
            'generated_at': 'not-a-time',
            'toast_shown': True,
            'mailboxes': [
                {'mailbox_id': 'qq_mail', 'status': 'EMPTY_TODAY', 'count': 0},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'last-run.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            result = self.health.check_last_digest(path, now_factory=lambda: now)

        self.assertFalse(result['ok'])
        self.assertEqual('stale:unknown', result['detail'])

    def test_last_digest_missing_file_is_explicit_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.health.check_last_digest(
                Path(directory) / 'last-run.json'
            )
        self.assertFalse(result['ok'])
        self.assertTrue(result['detail'].startswith('not_available:'))

    def test_optional_assistant_failure_does_not_fail_required_health(self):
        with mock.patch.object(
            self.health, 'check_environment', return_value={'ok': True}
        ), mock.patch.object(
            self.health, 'check_credentials', return_value={'ok': True}
        ), mock.patch.object(
            self.health,
            'check_mcp_tools',
            return_value={'ok': True, 'count': 28, 'expected_count': 28, 'names': []},
        ), mock.patch.object(
            self.health,
            'check_scheduled_task',
            return_value={'ok': True, 'enabled': True, 'status': 'Ready', 'last_result': 0},
        ), mock.patch.object(
            self.health,
            'check_task_definition',
            return_value={'ok': True, 'detail': 'definition_matches', 'differences': []},
        ), mock.patch.object(
            self.health,
            'check_last_digest',
            return_value={'ok': True, 'detail': 'ok', 'last_run': {'toast_shown': True}},
        ), mock.patch.object(
            self.health,
            'check_assistant_server',
            return_value={'ok': False, 'running': False, 'detail': 'not_running:ConnectionError'},
        ):
            report = self.health.collect_health(task_runner=lambda *args, **kwargs: None)

        self.assertTrue(report['ok'])
        self.assertFalse(report['checks'][-1]['ok'])

    def test_render_report_preserves_info_and_manual_check(self):
        report = {
            'ok': True,
            'checks': [
                {'name': 'required', 'ok': True, 'required': True},
                {'name': 'optional', 'ok': False, 'required': False},
            ],
        }
        rendered = self.health.render_report(report)
        self.assertIn('[PASS] required', rendered)
        self.assertIn('[INFO] optional', rendered)
        self.assertIn('Overall: PASS', rendered)
        self.assertIn('MANUAL CHECK:', rendered)


if __name__ == '__main__':
    unittest.main()
