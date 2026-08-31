"""Security and flow tests for the local mail assistant web server."""

from __future__ import annotations

import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock

import windows_gui.mail_assistant as mail_assistant


def _load_server_module():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'mail_assistant_server.py'
    spec = importlib.util.spec_from_file_location('test_mail_assistant_server_target', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_startup_module():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'start_mail_assistant.py'
    spec = importlib.util.spec_from_file_location('test_mail_assistant_startup_target', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MailAssistantServerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.server = _load_server_module()

    def test_json_content_type_is_required_for_post(self):
        self.assertTrue(self.server.is_json_request('application/json; charset=utf-8'))
        self.assertFalse(self.server.is_json_request('text/plain'))

    def test_content_length_is_validated_and_bounded(self):
        parse = self.server.parse_content_length
        self.assertEqual((0, None), parse(None))
        self.assertEqual((2, None), parse('2'))
        self.assertEqual((0, 'invalid_content_length'), parse('bad'))
        self.assertEqual((0, 'invalid_content_length'), parse('-1'))
        self.assertEqual(
            (0, 'content_too_large'), parse(str(self.server.MAX_JSON_BODY_BYTES + 1))
        )

    def test_post_rejects_invalid_and_oversized_bodies_before_reading(self):
        cases = [
            ('invalid_content_length', 'bad', 400),
            ('content_too_large', str(self.server.MAX_JSON_BODY_BYTES + 1), 413),
        ]
        for expected_error, header, expected_code in cases:
            with self.subTest(header=header):
                handler = object.__new__(self.server.MailAssistantHandler)
                handler.path = '/unknown'
                handler.headers = {
                    'Host': '127.0.0.1:8931',
                    'Content-Type': 'application/json',
                    'Content-Length': header,
                }
                handler.rfile = io.BytesIO(b'{"must_not_read":true}')
                responses = []
                handler._send_json = lambda payload, code=0, responses=responses, expected_code=expected_code: (
                    responses.append((payload, code))
                )

                handler.do_POST()

                self.assertEqual(
                    [( {'error': expected_error}, expected_code)], responses
                )
                self.assertEqual(0, handler.rfile.tell())

    def test_foreign_browser_origin_is_rejected(self):
        self.assertTrue(self.server.is_local_request('127.0.0.1:8931'))
        self.assertTrue(
            self.server.is_local_request(
                '127.0.0.1:8931', 'http://localhost:8931/'
            )
        )

    def test_startup_refresh_uses_required_json_media_type(self):
        captured = []

        def capture_urlopen(request, timeout=None):
            captured.append(request)

        startup = _load_startup_module()
        with mock.patch.object(startup.urllib.request, 'urlopen', capture_urlopen):
            startup.trigger_refresh()

        self.assertEqual(1, len(captured))
        self.assertEqual(b'{}', captured[0].data)
        self.assertEqual('application/json', captured[0].headers.get('Content-type'))
        self.assertFalse(self.server.is_local_request('attacker.example:8931'))
        self.assertFalse(
            self.server.is_local_request(
                '127.0.0.1:8931', 'https://attacker.example'
            )
        )


class MailAssistantSendFlowTests(unittest.TestCase):
    def test_bachelor_send_creates_draft_before_smtp(self):
        account = {
            'host': 'imap.example.com',
            'port': '993',
            'username': 'student@example.com',
            'password': 'runtime-secret',
        }
        calls = []
        with mock.patch.object(
            mail_assistant, 'ensure_environment'
        ), mock.patch.object(
            mail_assistant, '_assistant_account', return_value=account
        ) as imap_account, mock.patch.object(
            mail_assistant,
            'save_draft_imap',
            side_effect=lambda *args, **kwargs: calls.append(('draft', args)) or '草稿箱',
        ), mock.patch.object(
            mail_assistant, 'SMTP_HOSTS', {'bachelor_mail': ('smtp.example.com', 465)}
        ), mock.patch.object(
            mail_assistant,
            'send_mail_smtp',
            side_effect=lambda *args, **kwargs: calls.append(('smtp', args)),
        ):
            detail = mail_assistant.send_mail_for_mailbox(
                'bachelor_mail',
                'teacher@example.com',
                'Subject',
                'Body',
            )

        self.assertEqual('draft', calls[0][0])
        self.assertEqual('smtp', calls[1][0])
        self.assertEqual(
            ['draft', 'smtp'], [call[0] for call in calls]
        )
        self.assertIn('草稿已保存到 草稿箱', detail)
        imap_account.assert_called_once_with(
            'bachelor_mail',
            mail_assistant.BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME,
        )


if __name__ == '__main__':
    unittest.main()
