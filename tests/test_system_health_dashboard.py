"""Tests for the shared side-effect-free dashboard health model."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from windows_gui import health_events
from windows_gui import system_health


class _SecretStore:
    def __init__(self, service: str, username: str) -> None:
        self.service = service
        self.username = username

    def get_secret(self) -> str:
        return 'present-but-never-returned'


class DashboardHealthTests(unittest.TestCase):
    def test_collect_dashboard_is_sanitized_and_side_effect_free(self):
        now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        tools = [SimpleNamespace(name=name) for name in system_health.STABLE_TOOL_NAMES]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            events_path = base / 'events.jsonl'
            last_run = base / 'last-run.json'
            last_attempt = base / 'last-attempt.json'
            last_run.write_text(
                '{"generated_at":"2026-09-02T11:00:00+00:00",'
                '"mailboxes":[{"status":"READY"}]}',
                encoding='utf-8',
            )
            last_attempt.write_text(
                '{"generated_at":"2026-09-02T11:00:00+00:00",'
                '"stage":"complete","ok":true}',
                encoding='utf-8',
            )
            health_events.record_health_event(
                'mail_assistant',
                'error',
                'assistant_request_failed',
                path=events_path,
                now_factory=lambda: now,
            )

            report = system_health.collect_dashboard_health(
                now_factory=lambda: now,
                assistant_running=True,
                events_path=events_path,
                mcp_list_tools=lambda: tools,
                credential_store_factory=_SecretStore,
                last_run_path=last_run,
                last_attempt_path=last_attempt,
                task_runner=lambda *args, **kwargs: SimpleNamespace(
                    returncode=0,
                    stdout='Scheduled Task State: Enabled\nStatus: Ready\nLast Result: 0\n',
                ),
            )

        self.assertTrue(report['side_effect_free'])
        self.assertEqual('assistant_request_failed', report['recent_errors'][0]['code'])
        self.assertNotIn('present-but-never-returned', str(report))
        mcp = next(item for item in report['components'] if item['component'] == 'mcp')
        self.assertEqual('PASS', mcp['status'])

    def test_missing_stable_tool_is_a_failure(self):
        checked_at = '2026-09-02T12:00:00+00:00'
        tools = [
            SimpleNamespace(name=name)
            for name in sorted(system_health.STABLE_TOOL_NAMES)[1:]
        ]

        result = system_health.check_mcp_component(
            checked_at, list_tools=lambda: tools
        )

        self.assertEqual('FAIL', result['status'])
        self.assertEqual(1, len(result['details']['missing_stable_tools']))

    def test_overall_status_uses_worst_observed_state(self):
        components = [
            {'status': 'PASS'}, {'status': 'UNKNOWN'}, {'status': 'WARN'}
        ]
        self.assertEqual('WARN', system_health._overall_status(components))
        components.append({'status': 'FAIL'})
        self.assertEqual('FAIL', system_health._overall_status(components))


if __name__ == '__main__':
    unittest.main()
