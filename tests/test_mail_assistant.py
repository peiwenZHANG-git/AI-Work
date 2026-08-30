"""Unit tests for the AI draft assistant helpers."""

import unittest
from types import SimpleNamespace

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
