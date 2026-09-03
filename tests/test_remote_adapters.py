"""Unit tests for remote staging adapters (ownership, approval, leakage)."""

import threading
import unittest

from windows_gui.remote import adapters as remote_adapters
from windows_gui.task_center import TaskCenterError
from windows_gui.remote.adapters import RemoteAdapters
from windows_gui.remote.protocol import RequestIdConflictError


def make_adapters(**overrides):
    clock = {'now': 1_000.0}
    events = []
    defaults = dict(
        now_factory=lambda: clock['now'],
        browser_click_executor=lambda text, exact: {'status': 'CLICKED'},
        browser_download_executor=lambda text, exact, filename: {
            'status': 'DOWNLOADED',
            'path': r'C:\Users\x\AppData\Local\AI-Work\remote-downloads\f.bin',
            'filename': 'f.bin',
            'size_bytes': 4,
            'sha256': 'a' * 64,
        },
        mail_draft_executor=lambda mailbox_id, to, subject, body: {
            'pending_id': 'pending-ref',
            'mailbox_id': mailbox_id,
            'detail': '草稿已保存',
        },
        download_root=r'C:\confinement\remote-downloads',
        audit=lambda code, **kwargs: events.append((code, kwargs)),
    )
    defaults.update(overrides)
    adapters = RemoteAdapters(**defaults)
    return adapters, events, clock


def fingerprint(command, params):
    import hashlib
    import json

    return hashlib.sha256(json.dumps(
        {'command': command, 'params': params},
        sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')).hexdigest()


class StagingOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.adapters, self.events, self.clock = make_adapters()

    def test_stage_returns_task_reference_and_registers_owner(self):
        result = self.adapters.stage(
            device_id='device-a', request_id='r1',
            command='browser.request_click',
            params={'text': 'Submit', 'exact': True},
            fingerprint=fingerprint('browser.request_click', {
                'text': 'Submit', 'exact': True,
            }),
        )
        self.assertEqual('STAGED', result['status'])
        tasks = self.adapters.list_device_tasks('device-a')
        self.assertEqual(1, len(tasks))
        self.assertEqual('remote_click', tasks[0]['action'])
        self.assertEqual('STAGED', tasks[0]['state'])

    def test_device_b_cannot_see_or_cancel_device_a_task(self):
        staged = self.adapters.stage(
            device_id='device-a', request_id='r1',
            command='browser.request_click',
            params={'text': 'Submit', 'exact': True},
            fingerprint=fingerprint('browser.request_click', {
                'text': 'Submit', 'exact': True,
            }),
        )
        self.assertEqual([], self.adapters.list_device_tasks('device-b'))
        self.assertFalse(self.adapters.cancel_device_task(
            'device-b', staged['task_id'],
        ))
        view = self.adapters.local_confirmations()
        self.assertEqual(1, len(view))

    def test_idempotent_stage_reuses_task_and_conflicts_on_payload_change(self):
        fp = fingerprint('mail.request_draft', {
            'mailbox_id': 'master_mail', 'to': 't@example.edu',
            'subject': 's', 'body': 'b',
        })
        params = {
            'mailbox_id': 'master_mail', 'to': 't@example.edu',
            'subject': 's', 'body': 'b',
        }
        first = self.adapters.stage(
            device_id='device-a', request_id='r1',
            command='mail.request_draft', params=params, fingerprint=fp,
        )
        second = self.adapters.stage(
            device_id='device-a', request_id='r1',
            command='mail.request_draft', params=params, fingerprint=fp,
        )
        self.assertEqual(first['task_id'], second['task_id'])
        changed = dict(params, subject='different')
        with self.assertRaises(RequestIdConflictError):
            self.adapters.stage(
                device_id='device-a', request_id='r1',
                command='mail.request_draft', params=changed,
                fingerprint=fingerprint('mail.request_draft', changed),
            )


class LocalApprovalTests(unittest.TestCase):
    def setUp(self):
        self.adapters, self.events, self.clock = make_adapters()

    def _stage_click(self, device_id='device-a', request_id='r1'):
        return self.adapters.stage(
            device_id=device_id, request_id=request_id,
            command='browser.request_click',
            params={'text': 'Submit', 'exact': True},
            fingerprint=fingerprint('browser.request_click', {
                'text': 'Submit', 'exact': True,
            }),
        )

    def test_approve_executes_once_and_marks_succeeded(self):
        staged = self._stage_click()
        result = self.adapters.approve_task(staged['task_id'])
        self.assertEqual({'status': 'CLICKED'}, result)
        view = self.adapters.list_device_tasks('device-a')[0]
        self.assertEqual('SUCCEEDED', view['state'])

    def test_double_approve_fails_closed(self):
        staged = self._stage_click()
        self.adapters.approve_task(staged['task_id'])
        with self.assertRaises(Exception):
            self.adapters.approve_task(staged['task_id'])

    def test_execution_failure_marks_task_failed(self):
        def broken(text, exact):
            raise RuntimeError('executor down')

        adapters, events, _ = make_adapters(browser_click_executor=broken)
        staged = adapters.stage(
            device_id='device-a', request_id='r1',
            command='browser.request_click',
            params={'text': 'Submit', 'exact': True},
            fingerprint=fingerprint('browser.request_click', {
                'text': 'Submit', 'exact': True,
            }),
        )
        with self.assertRaises(RuntimeError):
            adapters.approve_task(staged['task_id'])
        view = adapters.list_device_tasks('device-a')[0]
        self.assertEqual('FAILED', view['state'])

    def test_download_result_is_sanitized_for_return(self):
        staged = self.adapters.stage(
            device_id='device-a', request_id='r1',
            command='browser.request_download',
            params={'text': 'Download', 'exact': True, 'filename': 'f.bin'},
            fingerprint=fingerprint('browser.request_download', {
                'text': 'Download', 'exact': True, 'filename': 'f.bin',
            }),
        )
        result = self.adapters.approve_task(staged['task_id'])
        self.assertNotIn('path', result)
        self.assertEqual('f.bin', result['filename'])
        self.assertEqual(4, result['size_bytes'])

    def test_local_confirmations_expose_summary_not_params(self):
        self.adapters.stage(
            device_id='device-a', request_id='r1',
            command='mail.request_draft',
            params={
                'mailbox_id': 'master_mail', 'to': 't@example.edu',
                'subject': '秘密主题', 'body': '正文内容',
            },
            fingerprint=fingerprint('mail.request_draft', {
                'mailbox_id': 'master_mail', 'to': 't@example.edu',
                'subject': '秘密主题', 'body': '正文内容',
            }),
        )
        view = self.adapters.local_confirmations()
        self.assertEqual(1, len(view))
        self.assertEqual('device-a', view[0]['device_id'])
        rendered = repr(view)
        self.assertNotIn('正文内容', rendered)

    def test_revoke_device_cancels_staged_tasks(self):
        staged = self._stage_click()
        cancelled = self.adapters.revoke_device_tasks('device-a')
        self.assertEqual(1, cancelled)
        view = self.adapters.list_device_tasks('device-a')[0]
        self.assertEqual('CANCELLED', view['state'])
        with self.assertRaises(Exception):
            self.adapters.approve_task(staged['task_id'])


class RaceConditionTests(unittest.TestCase):
    def setUp(self):
        self.clock = {'now': 1_000.0}
        self.adapters, self.events, _ = make_adapters(
            now_factory=lambda: self.clock['now'],
        )

    def _stage_click(self):
        return self.adapters.stage(
            device_id='device-a', request_id='r1',
            command='browser.request_click',
            params={'text': 'Submit', 'exact': True},
            fingerprint=fingerprint('browser.request_click', {
                'text': 'Submit', 'exact': True,
            }),
        )

    def test_concurrent_approve_executes_exactly_once(self):
        staged = self._stage_click()
        results = []
        errors = []
        lock = threading.Lock()

        def approve():
            try:
                result = self.adapters.approve_task(staged['task_id'])
            except TaskCenterError as error:
                with lock:
                    errors.append(error)
            else:
                with lock:
                    results.append(result)

        threads = [threading.Thread(target=approve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, len(results))
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], TaskCenterError)
        view = self.adapters.list_device_tasks('device-a')[0]
        self.assertEqual('SUCCEEDED', view['state'])

    def test_revoke_wins_over_approve(self):
        staged = self._stage_click()
        self.adapters.revoke_device_tasks('device-a')
        with self.assertRaises(Exception):
            self.adapters.approve_task(staged['task_id'])

    def test_expiry_wins_over_approve(self):
        params = {
            'mailbox_id': 'master_mail', 'to': 't@example.edu',
            'subject': 's', 'body': 'b',
        }
        task_id = self.adapters.stage(
            device_id='device-a', request_id='expiry-1',
            command='mail.request_draft', params=params,
            fingerprint=fingerprint('mail.request_draft', params),
        )
        self.clock['now'] += 1801
        with self.assertRaises(Exception):
            self.adapters.approve_task(task_id)
        view = self.adapters.list_device_tasks('device-a')
        self.assertEqual([], [t for t in view if t['state'] == 'STAGED'])


if __name__ == '__main__':
    unittest.main()
