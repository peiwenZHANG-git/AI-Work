"""Tests for bounded, non-sensitive health event storage."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from windows_gui import health_events
from windows_gui.health_events import read_health_events, record_health_event


class HealthEventTests(unittest.TestCase):
    def test_reply_draft_events_are_allowlisted(self):
        expected = {
            'reply_draft_generated': ('success', 'AI reply draft generated.'),
            'reply_draft_fallback': (
                'warning', 'Local fallback reply draft generated.'
            ),
            'reply_draft_remote_failed': (
                'error', 'Remote AI reply draft generation failed.'
            ),
        }
        for code, (outcome, expected_summary) in expected.items():
            with self.subTest(code=code):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'events.jsonl'
                    self.assertTrue(record_health_event(
                        'mail_assistant', outcome, code, path=path
                    ))
                    item = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(expected_summary, item['summary'])

    def test_event_schema_is_allowlisted_and_timezone_aware(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            self.assertTrue(record_health_event(
                'mail_assistant', 'error', 'assistant_request_failed',
                path=path,
                now_factory=lambda: datetime(2026, 9, 2, 17, 0, tzinfo=timezone.utc),
            ))
            raw = path.read_text(encoding='utf-8')
            self.assertNotIn('token', raw.casefold())
            item = json.loads(raw)
            self.assertEqual(
                {'component', 'outcome', 'code', 'summary', 'time'}, set(item)
            )
            self.assertEqual('+00:00', item['time'][-6:])

    def test_browser_worker_events_are_allowlisted(self):
        expected = {
            'worker_recovered': (
                'success', 'Browser session worker auto-recovered.'
            ),
            'worker_recovery_failed': (
                'error', 'Browser session worker auto-recovery failed.'
            ),
        }
        for code, (outcome, expected_summary) in expected.items():
            with self.subTest(code=code):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'events.jsonl'
                    self.assertTrue(record_health_event(
                        'browser_session', outcome, code, path=path
                    ))
                    item = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(expected_summary, item['summary'])

    def test_remote_events_are_allowlisted_with_optional_device(self):
        expected = {
            'remote_auth_failed': ('error', 'Remote authentication failed.'),
            'remote_task_staged': ('success', 'Remote task was staged.'),
            'remote_device_revoked': (
                'warning', 'Remote device was revoked.'
            ),
        }
        for code, (outcome, expected_summary) in expected.items():
            with self.subTest(code=code):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'events.jsonl'
                    self.assertTrue(record_health_event(
                        'remote', outcome, code, path=path,
                        device='0123456789abcdef',
                    ))
                    item = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(expected_summary, item['summary'])
                self.assertEqual('0123456789abcdef', item['device'])

    def test_remote_device_hash_must_be_sixteen_hex(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            self.assertFalse(record_health_event(
                'remote', 'error', 'remote_auth_failed', path=path,
                device='NOT-HEX',
            ))
            self.assertFalse(path.exists())

    def test_reader_preserves_remote_device_field(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            self.assertTrue(record_health_event(
                'remote', 'success', 'remote_session_created', path=path,
                device='abcdef0123456789',
                now_factory=lambda: datetime(
                    2026, 9, 3, 12, 0, tzinfo=timezone.utc,
                ),
            ))
            report = read_health_events(path)
        self.assertEqual(1, len(report['events']))
        self.assertEqual(
            'abcdef0123456789', report['events'][0].get('device')
        )

    def test_unknown_code_or_component_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            self.assertFalse(record_health_event('secret', 'error', 'raw-error', path=path))
            self.assertFalse(path.exists())

    def test_reader_skips_corruption_and_bounds_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            valid = {
                'component': 'mail_digest', 'outcome': 'error',
                'code': 'digest_failed', 'summary': 'ignored',
                'time': '2026-09-02T10:00:00+02:00',
            }
            path.write_text(
                'bad json\n' + json.dumps(valid) + '\n' +
                json.dumps({**valid, 'time': '2026-09-02T10:00:00'}) + '\n',
                encoding='utf-8',
            )
            report = read_health_events(path, limit=1, rotations=0)
            self.assertEqual(1, len(report['events']))
            self.assertEqual(2, report['invalid_lines'])
            self.assertEqual('Daily mail digest failed.', report['events'][0]['summary'])

    def test_rotation_keeps_bounded_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            for _ in range(8):
                self.assertTrue(record_health_event(
                    'mail_digest', 'success', 'digest_completed',
                    path=path, max_bytes=1, rotations=2,
                ))
            self.assertTrue(path.exists())
            self.assertTrue(Path(f'{path}.1').exists())
            self.assertTrue(Path(f'{path}.2').exists())
            self.assertFalse(Path(f'{path}.3').exists())

    def test_writer_failure_is_fail_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            parent_file = Path(directory) / 'not-a-directory'
            parent_file.write_text('x', encoding='utf-8')
            self.assertFalse(record_health_event(
                'mail_digest', 'error', 'digest_failed',
                path=parent_file / 'events.jsonl',
            ))

    def test_lock_failure_never_interrupts_primary_work(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            health_events,
            '_cross_process_lock',
            side_effect=RuntimeError('mutex unavailable'),
        ):
            path = Path(directory) / 'events.jsonl'
            self.assertFalse(record_health_event(
                'mail_digest', 'error', 'digest_failed', path=path
            ))
            self.assertEqual(
                {'events': [], 'invalid_lines': 1},
                read_health_events(path),
            )
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
