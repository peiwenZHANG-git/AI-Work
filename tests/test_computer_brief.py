"""Side-effect-free morning brief coverage using only owned temporary files."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, mock
import json

from windows_gui import computer_brief as brief
from windows_gui.mail_backends import BackendEmail, BackendStatus, MailBackendResult
from windows_gui.system_status import collect_status, power_result
from scripts import install_scheduled_tasks as scheduler

NOW = datetime(2026, 9, 6, 8, tzinfo=timezone.utc)


def system():
    return {'status': 'ok', 'battery': {'status': 'ok', 'state': 'not_present', 'percent': None},
            'disks': {'status': 'ok', 'volumes': []},
            'foreground_window': {'status': 'unknown'}, 'screen': {'status': 'unknown'}}


def model():
    return {'generated_at': NOW.isoformat(), 'date': '2026-09-06',
            'mailboxes': [{'mailbox_id': name, 'status': 'ok', 'code': 'EMPTY_TODAY',
                          'today_count': 0, 'important_count': 0, 'count_scope': 'confirmed_empty',
                          'items': []} for name in brief.MAILBOX_IDS],
            'downloads': {'status': 'ok', 'partial': False, 'recent_count': 0, 'items': []},
            'system': system(), 'suggestions': ['无明显事项']}


class MailTests(TestCase):
    def test_three_success_no_write_or_browser_or_llm(self):
        backend = mock.Mock()
        backend.summarize_today.return_value = MailBackendResult(BackendStatus.READY, 'ignored', (
            BackendEmail('secret@example.com', 'Urgent deadline', '', 'BODY SECRET'),))
        with mock.patch('windows_gui.mail_summary._summarize_with_edge') as browser, \
             mock.patch('windows_gui.mail_digest.call_mail_summary') as llm:
            boxes = brief.collect_mail(backend_factory=lambda identity: backend)
        self.assertEqual([b['mailbox_id'] for b in boxes], list(brief.MAILBOX_IDS))
        self.assertTrue(all(b['today_count'] == b['important_count'] == 1 for b in boxes))
        self.assertTrue(all(b['count_scope'] == 'bounded_metadata' for b in boxes))
        self.assertNotIn('BODY SECRET', json.dumps(boxes))
        self.assertNotIn('secret@example.com', json.dumps(boxes))
        self.assertEqual([call[0] for call in backend.method_calls], ['summarize_today'] * 3)
        browser.assert_not_called()
        llm.assert_not_called()

    def test_empty_failure_and_auth_are_distinct(self):
        statuses = iter([BackendStatus.EMPTY_TODAY, BackendStatus.IMAP_NETWORK_FAILED,
                         BackendStatus.IMAP_AUTH_FAILED])
        boxes = brief.collect_mail(backend_factory=lambda identity: SimpleNamespace(
            summarize_today=lambda limit: MailBackendResult(next(statuses), 'SECRET ERROR')))
        self.assertEqual(boxes[0]['today_count'], 0)
        self.assertIsNone(boxes[1]['today_count'])
        self.assertEqual(boxes[1]['status'], 'unavailable')
        self.assertEqual(boxes[2]['status'], 'auth/config error')
        self.assertNotIn('SECRET', json.dumps(boxes))

    def test_backend_exception_is_isolated(self):
        def factory(identity):
            if identity.mailbox_id == 'master_mail':
                raise RuntimeError('token=SECRET')
            return SimpleNamespace(summarize_today=lambda limit: MailBackendResult(BackendStatus.EMPTY_TODAY, ''))
        boxes = brief.collect_mail(backend_factory=factory)
        self.assertEqual([b['today_count'] for b in boxes], [0, None, 0])
        self.assertNotIn('SECRET', json.dumps(boxes))


class DownloadsTests(TestCase):
    def entry(self, name, hours):
        return {'path': 'Downloads/' + name, 'type': 'file', 'size': 12,
                'modified_time': (NOW - timedelta(hours=hours)).isoformat()}

    def test_time_limit_output_limit_and_no_content_read(self):
        scan = mock.Mock(return_value={'status': 'ok', 'partial': False, 'entries': [
            self.entry('old.pdf', 25), self.entry('future.pdf', -1),
            *[self.entry(f'{i}.pdf', i) for i in range(12)]]})
        with mock.patch('windows_gui.files._read', side_effect=AssertionError('no content')):
            result = brief.collect_downloads(NOW, inspector=scan)
        self.assertEqual(result['recent_count'], 12)
        self.assertEqual(len(result['items']), 10)
        self.assertTrue(result['display_truncated'])
        self.assertNotIn('old.pdf', str(result))
        self.assertEqual(scan.call_args.args[0]['operation'], 'list')

    def test_partial_and_unavailable(self):
        for flag in ('partial', 'results_truncated'):
            result = brief.collect_downloads(NOW, inspector=lambda request: {
                'status': 'ok', flag: True, 'entries': []})
            self.assertEqual(result['status'], 'partial')
        result = brief.collect_downloads(NOW, inspector=mock.Mock(side_effect=OSError('SECRET')))
        self.assertIsNone(result['recent_count'])
        self.assertNotIn('SECRET', str(result))

    def test_reject_absolute_and_traversal(self):
        for path in ('C:/Users/person/file.pdf', 'Downloads/../file.pdf', 'Downloads/x:y'):
            entry = self.entry('x', 1)
            entry['path'] = path
            result = brief.collect_downloads(NOW, inspector=lambda request: {'status': 'ok', 'entries': [entry]})
            self.assertEqual(result['status'], 'unavailable')
            self.assertNotIn(path, str(result))

    def test_real_internal_scan_owned_fixture_no_readfile(self):
        from windows_gui.files import inspect
        from windows_gui.local_paths import PathPolicy
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sample.pdf').write_bytes(b'fixture')
            with mock.patch('windows_gui.files.win32file.ReadFile', side_effect=AssertionError('no body')):
                result = brief.collect_downloads(datetime.now().astimezone(), inspector=lambda request:
                    inspect(request, policy=PathPolicy({'Downloads': root})))
            self.assertEqual(result['items'][0]['filename'], 'Downloads/sample.pdf')


class SystemAndSuggestionsTests(TestCase):
    def test_battery_states_partial_disk_and_bounded_title(self):
        backend = SimpleNamespace(battery=lambda: power_result(0, 0, 15),
            foreground=lambda: {'status': 'ok', 'title': 'x' * 400},
            disks=lambda: {'status': 'unknown', 'volumes': [{'drive': 'C:', 'status': 'unknown'}]},
            screen=lambda: {'status': 'ok', 'primary': {'width': 1920, 'height': 1080}},
            mouse=lambda: {'status': 'ok', 'x': 1, 'y': 2})
        result = brief.collect_system(collector=lambda: collect_status(backend=backend))
        self.assertEqual(result['battery']['percent'], 15)
        self.assertEqual(len(result['foreground_window']['title']), 256)
        self.assertEqual(result['disks']['status'], 'unknown')
        self.assertNotIn('free_bytes', result['disks']['volumes'][0])
        self.assertEqual(power_result(1, 128, 255)['state'], 'not_present')
        self.assertIsNone(power_result(255, 255, 255).get('percent'))

    def test_all_rules_and_max_three(self):
        data = model()
        self.assertIn('没有明显', brief.suggestions(data)[0])
        data['mailboxes'][0]['important_count'] = 1
        self.assertIn('重要邮件', brief.suggestions(data)[0])
        data['downloads']['items'] = [{'file_type': '.pdf'}]
        self.assertTrue(any('PDF' in s for s in brief.suggestions(data)))
        data['system']['battery'] = {'status': 'ok', 'percent': 10}
        self.assertTrue(any('电量' in s for s in brief.suggestions(data)))
        data['system']['disks']['volumes'] = [{'status': 'ok', 'total_bytes': 100, 'free_bytes': 5}]
        self.assertEqual(len(brief.suggestions(data)), 3)
        self.assertTrue(any('磁盘' in s for s in brief.suggestions(data)))

    def test_unknown_not_all_clear(self):
        data = model()
        data['downloads']['status'] = 'partial'
        self.assertIn('不完整', brief.suggestions(data)[0])


class ArtifactTests(TestCase):
    def test_dry_run_no_artifacts_or_notification(self):
        with mock.patch.object(brief, 'atomic_write') as write, mock.patch.object(brief, 'notify_brief') as notify:
            self.assertTrue(brief.run(dry_run=True, collector=model)['ok'])
        write.assert_not_called()
        notify.assert_not_called()

    def test_malformed_old_artifact_and_notification_degradation(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'latest.json').write_text('{invalid')
            result = brief.run(output_dir=root, collector=model, notifier=mock.Mock(side_effect=OSError('SECRET')))
            self.assertTrue(result['ok'])
            self.assertEqual(result['component_status']['notification'], 'degraded')
            self.assertEqual(json.loads((root / 'latest.json').read_text(encoding='utf-8'))['date'], '2026-09-06')
            diagnostic = (root / 'last-attempt.json').read_text()
            self.assertNotIn('SECRET', diagnostic)
            self.assertNotIn('subject', diagnostic)
            self.assertIn('今日重点', (root / 'latest.html').read_text(encoding='utf-8'))

    def test_atomic_failure_preserves_previous_file(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'latest.json'
            path.write_text('previous')
            with mock.patch.object(brief.os, 'replace', side_effect=PermissionError('SECRET')):
                with self.assertRaises(PermissionError):
                    brief.atomic_write(path, 'new')
            self.assertEqual(path.read_text(), 'previous')
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_collection_failure_fixed_diagnostic(self):
        with TemporaryDirectory() as folder:
            result = brief.run(output_dir=Path(folder), collector=mock.Mock(side_effect=RuntimeError('SECRET')))
            self.assertFalse(result['ok'])
            self.assertEqual(result['code'], 'collection_failed')
            self.assertNotIn('SECRET', str(result))

    def test_redaction_html_escaping(self):
        value = brief.safe_text('hello https://host/token token=SECRET file://hidden/path edge://settings x@example.com C:\\private\\data')
        for secret in ('SECRET', 'https:', 'file://', 'edge://', 'example.com', 'private'):
            self.assertNotIn(secret, value)
        data = model()
        data['suggestions'] = ['<script>alert(1)</script>']
        output = brief.render_html(data)
        self.assertNotIn('<script>', output)
        self.assertIn('&lt;script&gt;', output)


class SchedulerTests(TestCase):
    def test_brief_and_digest_definitions(self):
        args = {'root': Path('C:/repo'), 'python_executable': 'C:/Python/python.exe'}
        legacy = scheduler.build_install_script(**args)
        script = scheduler.build_install_script(**args, computer_brief=True)
        self.assertIn("-Daily -At '08:00'", script)
        self.assertNotIn('10:00', script)
        self.assertIn('-Minutes 15', script)
        self.assertIn('-MultipleInstances IgnoreNew', script)
        self.assertIn('-LogonType Interactive -RunLevel Limited', script)
        self.assertIn('-AllowStartIfOnBatteries `', script)
        self.assertNotIn('Start-ScheduledTask', script)
        self.assertIn("-Daily -At '10:00'", legacy)
        self.assertIn("-Daily -At '22:00'", legacy)
        self.assertNotIn('08:00', legacy)

    def test_check_drift_and_dry_run(self):
        args = {'root': Path('C:/repo'), 'python_executable': 'C:/Python/python.exe', 'computer_brief': True}
        desired = scheduler.expected_task_definition(**args)
        actual = dict(desired, trigger_times='08:00')
        runner = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(actual)))
        self.assertTrue(scheduler.check_task_definition(**args, runner=runner)['ok'])
        self.assertIn(scheduler.BRIEF_TASK_NAME, runner.call_args.args[0][-1])
        for key, wrong in [('daily_trigger_count', 0), ('trigger_count', 2),
                           ('action_count', 2), ('daily_interval', 2),
                           ('repetition_disabled', False), ('enabled', False),
                           ('principal_current_user', False), ('logon_type', 'Password'),
                           ('run_level', 'Highest'), ('start_when_available', False),
                           ('execution_time_limit', 'PT1H'), ('multiple_instances', 'Parallel')]:
            changed = dict(actual)
            changed[key] = wrong
            self.assertIn(key, scheduler.compare_task_definitions(desired, changed))
        actual['trigger_times'] = '09:00'
        runner.return_value.stdout = json.dumps(actual)
        self.assertFalse(scheduler.check_task_definition(**args, runner=runner)['ok'])
        with mock.patch.object(scheduler, 'install_task') as install, mock.patch('builtins.print'):
            self.assertEqual(scheduler.main(['--computer-brief', '--dry-run']), 0)
        install.assert_not_called()
