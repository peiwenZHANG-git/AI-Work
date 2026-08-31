"""Unit tests for the AI draft assistant helpers."""

import unittest
from types import SimpleNamespace
import unittest.mock as mock

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
