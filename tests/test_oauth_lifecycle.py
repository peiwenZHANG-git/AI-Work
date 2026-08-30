"""Tests for safe, process-local rotation of the Outlook refresh token."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest import mock

import requests

import windows_gui.mail_assistant as mail_assistant
import windows_gui.mail_digest as mail_digest


class RefreshMasterGraphTokenTests(unittest.TestCase):
    def test_rotation_is_locked_read_exchanged_then_written(self):
        events = []

        @contextmanager
        def fake_lock(timeout_seconds=10.0):
            events.append(f'lock-enter:{timeout_seconds}')
            try:
                yield
            finally:
                events.append('lock-exit')

        def fake_exchange(token, scope):
            events.append(f'exchange:{scope}')
            return {
                'access_token': 'runtime-access',
                'refresh_token': 'runtime-rotation',
            }

        with mock.patch.dict(
            'os.environ',
            {
                'AI_WORK_OUTLOOK_TENANT_ID': 'tenant',
                'AI_WORK_OUTLOOK_CLIENT_ID': 'client',
            },
        ), mock.patch.object(
            mail_digest,
            '_graph_refresh_lock',
            fake_lock,
        ), mock.patch.object(
            mail_digest,
            'read_master_refresh_token',
            side_effect=lambda: events.append('read'),
        ), mock.patch.object(
            mail_digest,
            'exchange_master_refresh_token',
            side_effect=fake_exchange,
        ), mock.patch.object(
            mail_digest,
            'write_master_refresh_token',
            side_effect=lambda token: events.append('write'),
        ):
            payload = mail_digest.refresh_master_graph_token('Mail.Read offline_access')

        self.assertEqual(
            [
                'lock-enter:10.0',
                'read',
                'exchange:Mail.Read offline_access',
                'write',
                'lock-exit',
            ],
            events,
        )
        self.assertEqual('runtime-access', payload['access_token'])

    def test_invalid_json_is_an_explicit_flow_error(self):
        response = SimpleNamespace(status_code=400, json=lambda: (_ for _ in ()).throw(ValueError()))
        with mock.patch.dict(
            'os.environ',
            {
                'AI_WORK_OUTLOOK_TENANT_ID': 'tenant',
                'AI_WORK_OUTLOOK_CLIENT_ID': 'client',
            },
        ), mock.patch.object(
            requests, 'post', return_value=response
        ), mock.patch.object(
            mail_digest, '_graph_refresh_lock', fake_noop_lock
        ), mock.patch.object(
            mail_digest, 'read_master_refresh_token', return_value='runtime-token'
        ), mock.patch.object(
            mail_digest, 'write_master_refresh_token'
        ) as write:
            with self.assertRaises(mail_digest.MailboxFlowError) as raised:
                mail_digest.refresh_master_graph_token()

        self.assertIn('Graph 令牌接口响应不是 JSON', str(raised.exception))
        write.assert_not_called()


@contextmanager
def fake_noop_lock(timeout_seconds=10.0):
    yield


class AssistantRefreshIntegrationTests(unittest.TestCase):
    def test_assistant_uses_shared_process_safe_refresh(self):
        payload = {'access_token': 'runtime-access'}
        with mock.patch.object(
            mail_assistant,
            'refresh_master_graph_token',
            return_value=payload,
        ) as refresh:
            token = mail_assistant._assistant_graph_token('Mail.ReadWrite')

        self.assertEqual('runtime-access', token)
        refresh.assert_called_once_with('Mail.ReadWrite')


if __name__ == '__main__':
    unittest.main()
