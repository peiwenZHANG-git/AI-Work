"""Tests for the shared four-state dashboard health model."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from windows_gui import system_health


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, service, username):
        self.username = username

    def get_secret(self):
        return 'configured'


class HealthDashboardTests(unittest.TestCase):
    @staticmethod
    def healthy_task_runner(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout='Scheduled Task State: Enabled\nStatus: Ready\nLast Result: 0\n',
        )

    def test_mcp_requires_exact_documented_tool_set(self):
        names = sorted(system_health.STABLE_TOOL_NAMES | {'future_tool'})
        component = system_health.check_mcp_component(
            NOW.isoformat(), lambda: [SimpleNamespace(name=name) for name in names]
        )
        self.assertEqual('FAIL', component['status'])
        self.assertEqual(len(names), component['details']['observed_tool_count'])
        self.assertEqual(['future_tool'], component['details']['unexpected_tools'])

    def test_credential_access_failure_is_unknown_not_missing(self):
        class BrokenStore:
            def __init__(self, service, username):
                pass
            def get_secret(self):
                raise OSError('backend unavailable')
        component = system_health.check_credential_component(
            NOW.isoformat(), BrokenStore
        )
        self.assertEqual('UNKNOWN', component['status'])
        self.assertTrue(all(
            value == 'unknown' for value in component['details']['entries'].values()
        ))

    def test_digest_requires_timezone_and_uses_reliable_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'last-run.json'
            attempt = root / 'last-attempt.json'
            run.write_text(json.dumps({
                'generated_at': '2026-09-02T10:00:00+00:00',
                'mailboxes': [{'status': 'READY'}],
            }), encoding='utf-8')
            attempt.write_text(json.dumps({
                'generated_at': '2026-09-02T10:00:00+00:00',
                'stage': 'complete', 'ok': True,
            }), encoding='utf-8')
            component = system_health.check_digest_component(
                NOW.isoformat(), last_run_path=run, last_attempt_path=attempt,
                now_factory=lambda: NOW,
            )
            self.assertEqual('PASS', component['status'])
            self.assertEqual('2026-09-02T10:00:00+00:00', component['last_success_at'])
            run.write_text(json.dumps({
                'generated_at': '2026-09-02T10:00:00',
                'mailboxes': [{'status': 'READY'}],
            }), encoding='utf-8')
            component = system_health.check_digest_component(
                NOW.isoformat(), last_run_path=run, last_attempt_path=attempt,
                now_factory=lambda: NOW,
            )
            self.assertEqual('UNKNOWN', component['status'])

    def test_browser_not_started_and_remote_are_unknown(self):
        with mock.patch.dict(os.environ, {
            'AI_WORK_QQ_CDP_ENDPOINT': '',
            'AI_WORK_BACHELOR_CDP_ENDPOINT': '',
        }, clear=False):
            browser = system_health.check_browser_component(NOW.isoformat())
        remote = system_health.check_remote_component(NOW.isoformat())
        self.assertEqual('UNKNOWN', browser['status'])
        self.assertEqual('UNKNOWN', remote['status'])

    def test_non_loopback_cdp_is_fail_without_active_probe(self):
        with mock.patch.dict(os.environ, {
            'AI_WORK_QQ_CDP_ENDPOINT': 'http://192.0.2.1:9222',
            'AI_WORK_BACHELOR_CDP_ENDPOINT': '',
        }, clear=False):
            browser = system_health.check_browser_component(NOW.isoformat())
        self.assertEqual('FAIL', browser['status'])
        self.assertFalse(browser['details']['active_probe'])

    def test_scheduled_digest_query_is_read_only_and_bounded(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout='Scheduled Task State: Enabled\nStatus: Ready\nLast Result: 0\n',
            )
        result = system_health.check_scheduled_digest(runner)
        self.assertEqual('PASS', result['status'])
        self.assertEqual('schtasks', calls[0][0][0])
        self.assertIn('/query', calls[0][0])
        self.assertNotIn('/run', calls[0][0])
        self.assertEqual(5, calls[0][1]['timeout'])

    def test_unparseable_scheduled_task_is_unknown(self):
        result = system_health.check_scheduled_digest(
            lambda *args, **kwargs: SimpleNamespace(
                returncode=0, stdout='localized output without known labels'
            )
        )
        self.assertEqual('UNKNOWN', result['status'])

    def test_collector_has_six_components_and_only_four_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'run.json').write_text(json.dumps({
                'generated_at': '2026-09-02T10:00:00+00:00',
                'mailboxes': [{'status': 'READY'}],
            }), encoding='utf-8')
            report = system_health.collect_dashboard_health(
                now_factory=lambda: NOW,
                assistant_running=True,
                events_path=root / 'missing.jsonl',
                mcp_list_tools=lambda: [
                    SimpleNamespace(name=name)
                    for name in system_health.STABLE_TOOL_NAMES
                ],
                credential_store_factory=FakeStore,
                last_run_path=root / 'run.json',
                last_attempt_path=root / 'attempt.json',
                task_runner=self.healthy_task_runner,
            )
        self.assertEqual(6, len(report['components']))
        self.assertTrue(report['side_effect_free'])
        self.assertTrue(all(
            item['status'] in system_health.STATUSES
            for item in report['components']
        ))
        self.assertEqual('UNKNOWN', report['overall_status'])


if __name__ == '__main__':
    unittest.main()
