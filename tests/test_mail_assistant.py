"""Unit tests for the AI draft assistant helpers."""

import unittest
from types import SimpleNamespace
import unittest.mock as mock
import hashlib

import windows_gui.mail_assistant as mail_assistant

from windows_gui.mail_assistant import (
    AssistantError,
    build_draft_message,
    extract_recipient,
    find_drafts_folder,
    generate_draft_via_ai,
)


class FakeConnection:
    def __init__(self, list_lines):
        self.list_lines = list_lines

    def list(self):
        return 'OK', self.list_lines


class MailAssistantTest(unittest.TestCase):
    def tearDown(self):
        mail_assistant.PENDING_DRAFTS.clear()

    def test_extract_recipient_finds_email(self) -> None:
        self.assertEqual(
            extract_recipient('给 teacher@cuc.edu.cn 写邮件问好'),
            'teacher@cuc.edu.cn',
        )
        self.assertEqual(extract_recipient('没有邮箱地址'), '')

    def test_recipient_validation_accepts_only_one_plain_address(self):
        self.assertEqual(
            'teacher@example.edu',
            mail_assistant.validate_recipient(' teacher@example.edu '),
        )
        for recipient in (
            '',
            'Display Name <teacher@example.edu>',
            'teacher@example.edu, other@example.edu',
            'teacher@example.edu\r\nBcc: attacker@example.edu',
            'not-an-email',
            'teacher@bad..example.edu',
            f'{"a" * 321}@example.edu',
        ):
            with self.subTest(recipient=recipient):
                with self.assertRaises(AssistantError):
                    mail_assistant.validate_recipient(recipient)

    def test_subject_and_body_validation_bounds_and_sanitizes(self):
        self.assertEqual(
            '主题 还有下文',
            mail_assistant.validate_subject(' 主题\r\n还有下文 '),
        )
        self.assertEqual(
            '第一行\n第二行',
            mail_assistant.validate_body(' 第一行\n第二行 '),
        )
        with self.assertRaises(AssistantError):
            mail_assistant.validate_subject('x' * 201)
        with self.assertRaises(AssistantError):
            mail_assistant.validate_body('x' * 50_001)
        with self.assertRaises(AssistantError):
            mail_assistant.validate_body('bad\x00body')

    def test_generate_draft_via_ai_parses_json(self) -> None:
        def fake_post(url, headers=None, json=None, timeout=None):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {'choices': [{'message': {
                    'content': '好的 {"subject": "会议提醒", "body": "内容"} 完成'
                }}]},
            )

        draft = generate_draft_via_ai('写一封会议提醒', 'key', transport=fake_post)
        self.assertEqual(draft['subject'], '会议提醒')
        self.assertEqual(draft['body'], '内容')

    def test_generate_draft_via_ai_rejects_invalid_response(self) -> None:
        def bad_post(url, headers=None, json=None, timeout=None):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {'choices': [{'message': {'content': '不会写'}}]},
            )

        with self.assertRaises(Exception):
            generate_draft_via_ai('写邮件', 'key', transport=bad_post)

    def test_generate_instruction_and_ai_fields_are_bounded(self):
        def transport_should_not_run(url, headers=None, json=None, timeout=None):
            raise AssertionError('oversized instruction must not reach the API')

        with self.assertRaises(AssistantError):
            generate_draft_via_ai(
                'x' * (mail_assistant.MAX_INSTRUCTION_CHARS + 1),
                'key',
                transport=transport_should_not_run,
            )

        def oversized_ai_body(url, headers=None, json=None, timeout=None):
            content = (
                '{"subject": "会议提醒", "body": "'
                + 'x' * (mail_assistant.MAX_BODY_CHARS + 1)
                + '"}'
            )
            return SimpleNamespace(
                status_code=200,
                json=lambda: {'choices': [{'message': {'content': content}}]},
            )

        with self.assertRaises(Exception):
            generate_draft_via_ai(
                '写邮件', 'key', transport=oversized_ai_body
            )

    def test_draft_save_rejects_invalid_recipient_before_network(self):
        with mock.patch.object(
            mail_assistant, 'ensure_environment'
        ), mock.patch.object(
            mail_assistant, '_assistant_account'
        ) as account, mock.patch.object(
            mail_assistant, 'create_master_draft'
        ) as create_draft:
            with self.assertRaises(AssistantError):
                mail_assistant.save_draft_for_mailbox(
                    'master_mail',
                    'teacher@example.edu\r\nBcc: attacker@example.edu',
                    'Subject',
                    'Body',
                )

        account.assert_not_called()
        create_draft.assert_not_called()

    def test_build_draft_message_normalizes_header_injection(self):
        message = build_draft_message(
            'me@example.com',
            'you@example.edu',
            'Subject\r\nBcc: attacker@example.edu',
            'Body',
        )
        self.assertEqual('Subject Bcc: attacker@example.edu', message['Subject'])
        self.assertIsNone(message['Bcc'])

    def test_master_draft_uses_separate_stage_and_confirm_calls(self):
        calls = []
        with mock.patch.object(
            mail_assistant, 'ensure_environment'
        ), mock.patch.dict(
            'os.environ', {'AI_WORK_OUTLOOK_MAILBOX': 'me@example.edu'}
        ), mock.patch.object(
            mail_assistant,
            '_assistant_graph_token',
            side_effect=['write-token', 'send-token'],
        ), mock.patch.object(
            mail_assistant,
            'verify_master_mailbox',
            side_effect=lambda token, mailbox: calls.append(('identity', token)),
        ), mock.patch.object(
            mail_assistant,
            'create_master_draft',
            side_effect=lambda token, to, subject, body: calls.append(
                ('draft', token)
            ) or 'draft-1',
        ) as create_draft, mock.patch.object(
            mail_assistant,
            'verify_master_staged_draft',
            side_effect=lambda token, draft_id, to, subject, body: calls.append(
                ('verify', token, draft_id)
            ),
        ), mock.patch.object(
            mail_assistant,
            'send_master_message',
            side_effect=lambda token, draft_id: calls.append(('send', token, draft_id)),
        ):
            staged = mail_assistant.stage_draft_for_mailbox(
                'master_mail',
                'teacher@example.edu',
                'Subject',
                'Body',
            )
            detail = mail_assistant.send_staged_draft(staged['pending_id'])

        self.assertTrue(staged['pending_id'])
        self.assertEqual(
            [
                ('identity', 'write-token'),
                ('draft', 'write-token'),
                ('identity', 'send-token'),
                ('verify', 'send-token', 'draft-1'),
                ('send', 'send-token', 'draft-1'),
            ],
            calls,
        )
        self.assertIn('已确认发送', detail)
        create_draft.assert_called_once()

    def test_pending_draft_token_is_single_use(self):
        pending_id = mail_assistant._store_pending_draft({
            'mailbox_id': 'unknown_mailbox',
        })
        with self.assertRaises(AssistantError):
            mail_assistant.send_staged_draft(pending_id)

    def test_pending_draft_expires_before_send(self):
        pending_id = mail_assistant._store_pending_draft(
            {'mailbox_id': 'master_mail'},
            now=100,
            ttl_seconds=60,
        )
        mail_assistant.PENDING_DRAFTS[pending_id]['_expires_at_mono'] = 99

        with self.assertRaises(AssistantError):
            mail_assistant.send_staged_draft(pending_id)

        self.assertNotIn(pending_id, mail_assistant.PENDING_DRAFTS)

    def test_pending_draft_capacity_evicts_oldest(self):
        first = mail_assistant._store_pending_draft(
            {'mailbox_id': 'master_mail'}, now=100, max_items=2
        )
        second = mail_assistant._store_pending_draft(
            {'mailbox_id': 'master_mail'}, now=101, max_items=2
        )
        third = mail_assistant._store_pending_draft(
            {'mailbox_id': 'master_mail'}, now=102, max_items=2
        )

        self.assertNotIn(first, mail_assistant.PENDING_DRAFTS)
        self.assertIn(second, mail_assistant.PENDING_DRAFTS)
        self.assertIn(third, mail_assistant.PENDING_DRAFTS)

    def test_bachelor_confirm_sends_existing_staged_draft(self):
        account = {
            'host': 'imap.example.com',
            'port': '993',
            'username': 'me@example.edu',
            'password': 'runtime-secret',
        }
        reference = {
            'folder': 'Drafts',
            'uidvalidity': '99',
            'uid': '11',
            'message_sha256': 'valid-hash',
        }

        with mock.patch.object(
            mail_assistant, 'ensure_environment'
        ), mock.patch.object(
            mail_assistant,
            '_assistant_account',
            return_value=account,
        ), mock.patch.object(
            mail_assistant, 'stage_draft_imap', return_value=reference
        ):
            staged = mail_assistant.stage_draft_for_mailbox(
                'bachelor_mail',
                'teacher@example.edu',
                'Subject',
                'Body',
            )

        existing_message = build_draft_message(
            'me@example.edu',
            'teacher@example.edu',
            'Subject',
            'Body',
        )
        sent_messages = []
        credential_usernames = []

        def capture_account(mailbox_id, credential_username):
            credential_usernames.append(credential_username)
            if credential_username == mail_assistant.BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME:
                pass
            return account

        with mock.patch.object(
            mail_assistant, 'ensure_environment'
        ), mock.patch.object(
            mail_assistant, '_assistant_account', side_effect=capture_account
        ), mock.patch.object(
            mail_assistant,
            'fetch_staged_draft_imap',
            return_value=existing_message,
        ), mock.patch.object(
            mail_assistant,
            'send_existing_email_smtp',
            side_effect=lambda account_arg, message: sent_messages.append(message),
        ):
            detail = mail_assistant.send_staged_draft(staged['pending_id'])

        self.assertIn('已确认发送本科邮箱已保存草稿', detail)
        self.assertEqual([existing_message], sent_messages)
        self.assertIn(
            mail_assistant.BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME,
            credential_usernames,
        )

    def test_no_fresh_field_smtp_bypass_exists(self):
        self.assertFalse(hasattr(mail_assistant, 'send_mail_smtp'))
        self.assertTrue(hasattr(mail_assistant, 'send_existing_email_smtp'))

    def test_graph_confirmation_requires_existing_draft(self):
        payload = {
            'subject': 'Subject',
            'toRecipients': [{'emailAddress': {'address': 'teacher@example.edu'}}],
            'body': {'contentType': 'Text', 'content': 'Body'},
        }

        def make_get(is_draft):
            def get(url, headers=None, timeout=None):
                self.assertIn('isDraft', url)
                return SimpleNamespace(status_code=200, json=lambda: dict(payload, isDraft=is_draft))
            return get

        with mock.patch.object(
            mail_assistant.requests, 'get', side_effect=make_get(True)
        ):
            mail_assistant.verify_master_staged_draft(
                'token', 'draft-1', 'teacher@example.edu', 'Subject', 'Body'
            )

        with mock.patch.object(
            mail_assistant.requests, 'get', side_effect=make_get(False)
        ):
            with self.assertRaises(AssistantError):
                mail_assistant.verify_master_staged_draft(
                    'token', 'draft-1', 'teacher@example.edu', 'Subject', 'Body'
                )

    def test_imap_confirmation_requires_draft_flag(self):
        message = build_draft_message(
            'me@example.edu', 'teacher@example.edu', 'Subject', 'Body'
        )
        raw = message.as_bytes()
        context = {
            'folder': 'Drafts',
            'uidvalidity': '99',
            'uid': '11',
            'message_sha256': hashlib.sha256(raw).hexdigest(),
            'to': 'teacher@example.edu',
            'subject': 'Subject',
            'body': 'Body',
        }
        account = {
            'host': 'imap.example.com',
            'port': '993',
            'username': 'me@example.edu',
            'password': 'runtime-secret',
        }

        class FakeConnection:
            def __init__(self, flags):
                self.flags = flags

            def login(self, username, password):
                return 'OK', [b'authenticated']

            def select(self, mailbox, readonly=False):
                self.readonly = readonly
                return 'OK', [b'Flags 1 UIDVALIDITY 99']

            def uid(self, command, uid, *args):
                metadata = (
                    b'11 (UID 11 FLAGS (' + self.flags + b') BODY[] {'
                    + str(len(raw)).encode()
                    + b'}'
                )
                return 'OK', [(metadata, raw), b')']

            def logout(self):
                return 'BYE', [b'logout']

        with mock.patch.object(
            mail_assistant,
            '_default_imap_factory',
            side_effect=lambda *args, **kwargs: FakeConnection(b'\\Draft'),
        ):
            verified = mail_assistant.fetch_staged_draft_imap(account, context)
        self.assertEqual('Body', verified.get_content().strip())

        with mock.patch.object(
            mail_assistant,
            '_default_imap_factory',
            side_effect=lambda *args, **kwargs: FakeConnection(b''),
        ):
            with self.assertRaises(AssistantError):
                mail_assistant.fetch_staged_draft_imap(account, context)

    def test_imap_staging_returns_stable_reference_and_requires_draft(self):
        message = build_draft_message(
            'me@example.edu', 'teacher@example.edu', 'Subject', 'Body'
        )
        raw = message.as_bytes()
        account = {
            'host': 'imap.example.com',
            'port': '993',
            'username': 'me@example.edu',
            'password': 'runtime-secret',
        }

        class FakeStagingConnection:
            def __init__(self, append_response, flags):
                self.append_response = append_response
                self.flags = flags
                self.appended = None
                self.readonly = None

            def login(self, username, password):
                return 'OK', [b'authenticated']

            def list(self):
                return 'OK', [b'(\\HasNoChildren \\Drafts) "/" Drafts']

            def append(self, mailbox, flags, date, data):
                self.appended = (mailbox, flags, data)
                return 'OK', self.append_response

            def select(self, mailbox, readonly=False):
                self.readonly = readonly
                return 'OK', [b'Flags 1 UIDVALIDITY 99']

            def uid(self, command, uid, *args):
                metadata = (
                    b'11 (UID 11 FLAGS (' + self.flags + b') BODY[] {'
                    + str(len(raw)).encode()
                    + b'}'
                )
                return 'OK', [(metadata, raw), b')']

            def logout(self):
                return 'BYE', [b'logout']

        good = FakeStagingConnection(
            [b'[APPENDUID 99 11] Draft appended'], b'\\Draft'
        )
        with mock.patch.object(
            mail_assistant,
            '_default_imap_factory',
            side_effect=lambda *args, **kwargs: good,
        ):
            reference = mail_assistant.stage_draft_imap(
                account['host'],
                account['port'],
                account['username'],
                account['password'],
                'me@example.edu',
                'teacher@example.edu',
                'Subject',
                'Body',
            )

        self.assertEqual('Drafts', reference['folder'])
        self.assertEqual('99', reference['uidvalidity'])
        self.assertEqual('11', reference['uid'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), reference['message_sha256'])
        self.assertEqual(('Drafts', '(\\Draft)', raw), good.appended)
        self.assertTrue(good.readonly)

        missing_uid = FakeStagingConnection([b'Draft appended'], b'\\Draft')
        with mock.patch.object(
            mail_assistant,
            '_default_imap_factory',
            side_effect=lambda *args, **kwargs: missing_uid,
        ):
            with self.assertRaises(AssistantError):
                mail_assistant.stage_draft_imap(
                    account['host'], account['port'], account['username'],
                    account['password'], 'me@example.edu', 'teacher@example.edu',
                    'Subject', 'Body',
                )

        not_draft = FakeStagingConnection(
            [b'[APPENDUID 99 11] Draft appended'], b''
        )
        with mock.patch.object(
            mail_assistant,
            '_default_imap_factory',
            side_effect=lambda *args, **kwargs: not_draft,
        ):
            with self.assertRaises(AssistantError):
                mail_assistant.stage_draft_imap(
                    account['host'], account['port'], account['username'],
                    account['password'], 'me@example.edu', 'teacher@example.edu',
                    'Subject', 'Body',
                )

    def test_build_draft_message_sets_headers(self) -> None:
        message = build_draft_message('me@example.com', 'you@example.com', '主题', '正文')
        self.assertEqual(message['To'], 'you@example.com')
        self.assertEqual(message['Subject'], '主题')
        self.assertIn('正文', message.get_content())

    def test_find_drafts_folder_prefers_draft_flag(self) -> None:
        connection = FakeConnection([
            b'(\\HasNoChildren) "/" INBOX',
            b'(\\HasNoChildren \\Drafts) "/" Drafts',
            b'(\\HasNoChildren) "/" "Sent Messages"',
        ])
        self.assertEqual(find_drafts_folder(connection), 'Drafts')

    def test_find_drafts_folder_accepts_chinese_name(self) -> None:
        connection = FakeConnection([
            b'(\\HasNoChildren) "/" INBOX',
            '(\\HasNoChildren) "/" "草稿箱"',
        ])
        self.assertEqual(find_drafts_folder(connection), '草稿箱')


if __name__ == '__main__':
    unittest.main()
